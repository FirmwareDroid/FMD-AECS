import os
import shutil
import tempfile
import subprocess
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from typing import Iterable, List, Optional

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def _copyfileobj_large(src_f, dst_f, bufsize: int) -> None:
    while True:
        data = src_f.read(bufsize)
        if not data:
            break
        dst_f.write(data)


def _do_copy(src_path: str,
             dst_path: str,
             bufsize: int = 16 * 1024 * 1024,
             preserve_mode: bool = False,
             use_reflink: bool = False,
             fallback_to_shutil: bool = True) -> None:
    """Perform a single copy operation. This is intended to be run inside a worker thread.

    Raises exceptions on failure.
    """
    if os.path.abspath(src_path) == os.path.abspath(dst_path):
        logger.debug("source and destination are the same: %s", src_path)
        return

    dstdir = os.path.dirname(dst_path) or "."
    fd_tmp, tmpname = tempfile.mkstemp(prefix=".tmp.copy.", dir=dstdir)
    os.close(fd_tmp)

    try:
        copied = False

        # Try os.copy_file_range (Linux, newer Python)
        try:
            if hasattr(os, "copy_file_range"):
                with open(src_path, "rb") as sf, open(tmpname, "wb") as df:
                    src_fd = sf.fileno()
                    dst_fd = df.fileno()
                    # loop until EOF
                    while True:
                        n = os.copy_file_range(src_fd, None, dst_fd, None, bufsize)
                        if n == 0:
                            break
                logging.info(f"Fast Copy copy_file_range: Copied {src_path} to {dst_path}")
                copied = True
        except Exception:
            copied = False

        # Try sendfile if available
        if not copied and hasattr(os, "sendfile"):
            try:
                with open(src_path, "rb") as sf, open(tmpname, "wb") as df:
                    sfd = sf.fileno()
                    dfd = df.fileno()
                    offset = 0
                    st = os.fstat(sfd)
                    remaining = st.st_size
                    while remaining > 0:
                        sent = os.sendfile(dfd, sfd, offset, remaining)
                        if sent == 0:
                            break
                        offset += sent
                        remaining -= sent
                logging.info(f"Fast Copy sendfile: Copied {src_path} to {dst_path}")
                copied = True
            except Exception:
                copied = False

        # Try cp --reflink=auto when requested
        if not copied and use_reflink and os.name == "posix":
            cp = shutil.which("cp")
            if cp:
                args = [cp, "--reflink=auto", src_path, tmpname]
                try:
                    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    logging.info(f"Copied file with cp command: {src_path} to {dst_path}")
                    copied = True
                except Exception:
                    copied = False

        # Fallback to streaming copy using a large buffer
        if not copied:
            if not fallback_to_shutil:
                raise OSError("No fast copy method available and fallback_to_shutil is False")
            with open(src_path, "rb") as sf, open(tmpname, "wb") as df:
                _copyfileobj_large(sf, df, bufsize)

        # Optionally preserve metadata
        if preserve_mode:
            try:
                shutil.copystat(src_path, tmpname)
            except Exception:
                logger.debug("preserve_mode: copystat failed for %s", tmpname)

        # Atomic replace
        os.replace(tmpname, dst_path)
        logger.debug("Fast Copy Atomic Replace: Copied %s -> %s", src_path, dst_path)
    except Exception:
        try:
            os.remove(tmpname)
        except Exception:
            pass
        raise


class CopyManager:
    """Manage non-blocking copies using a thread pool.

    Usage:
        mgr = CopyManager(workers=4)
        fut = mgr.submit(src, dst)
        # continue
        mgr.wait_all()
    """

    def __init__(self, workers: Optional[int] = None):
        if workers is None:
            workers = max(2, (os.cpu_count() or 2) * 2)
        self._executor = ThreadPoolExecutor(max_workers=workers)
        self._futures: List[Future] = []

    def submit(self, src: str, dst: str, **kwargs) -> Future:
        fut = self._executor.submit(_do_copy, src, dst, **kwargs)
        self._futures.append(fut)
        return fut

    def submit_many(self, pairs: Iterable[tuple], **kwargs) -> List[Future]:
        futs = []
        for src, dst in pairs:
            futs.append(self.submit(src, dst, **kwargs))
        return futs

    def wait_all(self, timeout: Optional[float] = None) -> List[Future]:
        """Wait for all scheduled copy tasks. Raises exceptions for failed tasks when calling result()."""
        done = []
        for fut in as_completed(list(self._futures), timeout=timeout):
            done.append(fut)
        # Ensure all futures finished (if timeout not given, as_completed will yield all)
        results = []
        for f in self._futures:
            results.append(f.result())
        return self._futures

    def shutdown(self, wait: bool = True):
        self._executor.shutdown(wait=wait)


# module-level default manager
_GLOBAL_MANAGER: Optional[CopyManager] = None


def get_global_manager(workers: Optional[int] = None) -> CopyManager:
    global _GLOBAL_MANAGER
    if _GLOBAL_MANAGER is None:
        _GLOBAL_MANAGER = CopyManager(workers=workers)
    return _GLOBAL_MANAGER


def schedule_copy(src: str, dst: str, **kwargs) -> Future:
    """Schedule a copy and return a Future. Use wait_for_all_copy_tasks() to wait."""
    mgr = get_global_manager()
    return mgr.submit(src, dst, **kwargs)


def schedule_copy_many(pairs: Iterable[tuple], **kwargs) -> List[Future]:
    mgr = get_global_manager()
    return mgr.submit_many(pairs, **kwargs)


def wait_for_all_copy_tasks(timeout: Optional[float] = None):
    mgr = get_global_manager()
    return mgr.wait_all(timeout=timeout)


def shutdown_global_manager(wait: bool = True):
    global _GLOBAL_MANAGER
    if _GLOBAL_MANAGER is not None:
        _GLOBAL_MANAGER.shutdown(wait=wait)
        _GLOBAL_MANAGER = None

