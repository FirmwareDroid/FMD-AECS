import logging
import os
import shutil
import tempfile
import subprocess
import errno

def _copyfileobj_large(src_f, dst_f, bufsize):
    # shutil.copyfileobj does the same but this avoids an extra function call
    while True:
        data = src_f.read(bufsize)
        if not data:
            break
        dst_f.write(data)

def copy_fast(src_path, dst_path,
              bufsize=16 * 1024 * 1024,
              preserve_mode=False,
              use_reflink=False,
              fallback_to_shutil=True):
    """
    Copy file from src_path to dst_path as fast as possible with fallbacks.

    Parameters:
      - src_path, dst_path: paths (strings)
      - bufsize: buffer size in bytes for streaming fallback (default 16 MiB)
      - preserve_mode: if True, copy permissions & metadata (uses shutil.copystat)
      - use_reflink: if True and 'cp' exists, attempt `cp --reflink=auto` as one option
      - fallback_to_shutil: if False do not do the final python fallback (will raise on failure)

    Returns: None (raises on error)
    """
    if os.path.abspath(src_path) == os.path.abspath(dst_path):
        return

    if os.path.islink(src_path):
        raise RuntimeError(f"{src_path} is a symbolic link")

    # Create an atomic temp path in the destination directory
    dstdir = os.path.dirname(dst_path) or "."
    fd_tmp, tmpname = tempfile.mkstemp(prefix=".tmp.copy.", dir=dstdir)
    os.close(fd_tmp)
    copied = False
    try:
        # Attempt copy_file_range (Linux & supported Python)
        try:
            if hasattr(os, "copy_file_range"):
                with open(src_path, "rb") as sf, open(tmpname, "wb") as df:
                    src_fd = sf.fileno()
                    dst_fd = df.fileno()
                    # Loop until EOF
                    while True:
                        # copy up to bufsize each call
                        try:
                            n = os.copy_file_range(src_fd, None, dst_fd, None, bufsize)
                        except TypeError:
                            # Some platforms/Python builds reject None offsets. Try explicit 0 offsets.
                            try:
                                n = os.copy_file_range(src_fd, 0, dst_fd, 0, bufsize)
                            except Exception:
                                # give up on this method for this platform
                                raise
                        if n == 0:
                            break
                # success if we reach here; copy_file_range preserves data, fall through to finalize
                logging.info(f"Copy success with copy_file_range for {src_path} -> {dst_path}")
                copied = True
            else:
                copied = False
        except (AttributeError, OSError, NotImplementedError, TypeError):
            copied = False

        # Try sendfile (zero-copy, works well on Linux; macOS has different semantics but Python exposes os.sendfile)
        if not copied and hasattr(os, "sendfile"):
            try:
                with open(src_path, "rb") as sf, open(tmpname, "wb") as df:
                    sfd = sf.fileno()
                    dfd = df.fileno()
                    offset = 0
                    st = os.fstat(sfd)
                    remaining = st.st_size
                    while remaining > 0:
                        try:
                            sent = os.sendfile(dfd, sfd, offset, remaining)
                        except TypeError:
                            # Some platforms may have different sendfile signatures; re-raise to fall back
                            raise
                        if sent == 0:
                            break
                        offset += sent
                        remaining -= sent
                logging.info(f"Copy success with sendfile for {src_path} -> {dst_path}")
                copied = True
            except (OSError, NotImplementedError, TypeError):
                copied = False

        # Try cp --reflink=auto on POSIX when requested and cp exists (fast copy-on-write when supported)
        if not copied and use_reflink and os.name == "posix":
            cp = shutil.which("cp")
            if cp:
                args = [cp, "--reflink=auto", src_path, tmpname]
                try:
                    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    logging.info(f"Copy success with cp for {src_path} -> {tmpname}")
                    copied = True
                except subprocess.CalledProcessError:
                    copied = False

        # Fallback: streaming copy with a large buffer (portable)
        if not copied:
            if not fallback_to_shutil:
                raise OSError("No fast copy method available and fallback_to_shutil is False")
            with open(src_path, "rb") as sf, open(tmpname, "wb") as df:
                # Use a big user-configurable buffer
                _copyfileobj_large(sf, df, bufsize)

        # optionally preserve mode/metadata (permissions, times)
        if preserve_mode:
            try:
                shutil.copystat(src_path, tmpname, follow_symlinks=False)
            except Exception:
                # ignore stat copy errors (permission denied, etc.)
                pass

        # atomic move into place
        os.replace(tmpname, dst_path)
    except Exception:
        # remove tmp on error
        try:
            os.remove(tmpname)
        except Exception:
            pass
        raise
    finally:
        if not copied:
            logging.error(f"Copy failed for {src_path} -> {dst_path} with all methods")
