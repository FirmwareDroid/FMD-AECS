import concurrent
import zipfile

from common_post_injector import handle_app_modules
import concurrent.futures
import glob
import logging
import os
import re
import shutil
import signal
import subprocess
import time
import tempfile
import hashlib
from pathlib import Path
from urllib.parse import urlparse

from common import upload_build_artefact
from config import (
    PATH_BUILD_ACV_ERROR_LOG,
    PATH_BUILD_ACV_LOG,
    BUILD_OUT_PATH
)
from json_writer import write_json_output, write_json_nd_output
POST_INJECTOR_CONFIG = {}

def _acv_instrument_worker(params):
    """Worker called in a separate process to instrument a single APK."""
    apk_path, firmware_folder, acv_executable, safe_cwd = params
    filename = os.path.basename(apk_path)
    current_cwd = os.path.abspath(os.getcwd())
    start = None
    proc = None
    out_folder = ""

    try:
        base_dir = Path(apk_path).parent.name
        out_folder = os.path.join(firmware_folder, base_dir)

        # Ensure unique output folder
        idx = 1
        while os.path.exists(out_folder):
            out_folder = os.path.join(firmware_folder, f"{base_dir}_{idx}")
            idx += 1

        os.makedirs(out_folder, exist_ok=True)
        os.chdir(safe_cwd)

        # Determine retry attempts from configuration (best-effort). Default to 1 (no retry).
        try:
            max_attempts = int(POST_INJECTOR_CONFIG.get('ACV_INSTRUMENT_RETRY_ATTEMPTS', 1) or 3)
        except Exception:
            max_attempts = 1
        if max_attempts < 1:
            max_attempts = 1

        last_out_decoded = ""
        tried_method = False
        tried_class = False
        backup_path = None

        # Single attempt: primary instrumentation, then method fallback, then class fallback
        attempt_out = out_folder
        # Prepare workspace
        if os.path.exists(attempt_out):
            try:
                shutil.rmtree(attempt_out)
            except Exception:
                pass
        os.makedirs(attempt_out, exist_ok=True)

        # Backup original APK before attempting instrumentation
        try:
            backup_path = f"{apk_path}.acvbackup"
            shutil.copy2(apk_path, backup_path)
            logging.info("Backed up APK %s to %s before instrumentation", apk_path, backup_path)
        except Exception as e:
            logging.error("Failed to backup APK %s before instrumentation: %s", apk_path, e)
            return (filename, 0.0, "failed", f"BackupFailed: {e}", os.path.basename(attempt_out))

        # Primary instrumentation
        cmd = [acv_executable, "instrument", "-f", apk_path, "--wd", attempt_out]
        start = time.time()
        proc = subprocess.Popen(cmd,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                start_new_session=True,
                                cwd="/tmp/"
                                )
        try:
            out, _ = proc.communicate(timeout=700)
            elapsed = round(time.time() - start, 2)
            out_decoded = out.decode(errors='ignore') if out else ""

            if proc.returncode == 0:
                # success - remove backup if present
                try:
                    if backup_path and os.path.exists(backup_path):
                        os.remove(backup_path)
                        logging.info("Removed APK backup %s after successful instrumentation", backup_path)
                except Exception:
                    pass
                return (filename, elapsed, "success", "", os.path.basename(attempt_out))

            # failed attempt — proceed to fallbacks
            last_out_decoded = out_decoded
            logging.warning("ACVTool instrumentation failed for %s (returncode=%s).", filename, proc.returncode)

        except subprocess.TimeoutExpired:
            logging.error("ACVTool instrumentation for %s timed out after 700 seconds. Attempting to terminate process group.", filename)
            elapsed = round(time.time() - start, 2)
            _kill_process_group(proc)

            # Allow a short grace period to collect output
            try:
                out, _ = proc.communicate(timeout=5)
            except Exception:
                out = None

            # Ensure death
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass

            out_decoded = out.decode(errors='ignore') if out else ""
            last_out_decoded = f"TimeoutExpired: {out_decoded}"

            # Restore APK from backup if present
            try:
                if backup_path and os.path.exists(backup_path):
                    shutil.copy2(backup_path, apk_path)
                    logging.info("Restored APK %s from backup %s after timeout", apk_path, backup_path)
                    try:
                        os.remove(backup_path)
                    except Exception:
                        pass
            except Exception as e:
                logging.error("Failed to restore APK from backup after timeout: %s", e)

            return (filename, elapsed, "failed", last_out_decoded, os.path.basename(attempt_out))

        except Exception as e:
            # Capture any unexpected exception and fail
            elapsed = round(time.time() - start, 2) if start else 0.0
            try:
                out, _ = proc.communicate(timeout=1)
                out_decoded = out.decode(errors='ignore') if out else ""
            except Exception:
                out_decoded = ""
            last_out_decoded = f"Exception: {e} {out_decoded}"

            # Restore APK from backup if present
            try:
                if backup_path and os.path.exists(backup_path):
                    shutil.copy2(backup_path, apk_path)
                    logging.info("Restored APK %s from backup %s after exception", apk_path, backup_path)
                    try:
                        os.remove(backup_path)
                    except Exception:
                        pass
            except Exception as re:
                logging.error("Failed to restore APK from backup after exception: %s", re)

            logging.exception("ACVTool instrumentation raised exception for %s", filename)
            return (filename, elapsed, "failed", last_out_decoded, os.path.basename(attempt_out))

        # Method-level fallback
        logging.info("Attempting fallback ACVTool instrumentation using method group for %s", filename)
        fb_cmd = [acv_executable, "instrument", "-g", "method", "-f", apk_path, "--wd", attempt_out]
        fb_start = time.time()
        fb_proc = None
        try:
            fb_proc = subprocess.Popen(fb_cmd,
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT,
                                       start_new_session=True,
                                       cwd="/tmp/")
            fb_out, _ = fb_proc.communicate(timeout=700)
            fb_elapsed = round(time.time() - fb_start, 2)
            fb_out_decoded = fb_out.decode(errors='ignore') if fb_out else ""

            if fb_proc.returncode == 0:
                logging.info("Fallback method instrumentation succeeded for %s", filename)
                try:
                    if backup_path and os.path.exists(backup_path):
                        os.remove(backup_path)
                except Exception:
                    pass
                return (filename, fb_elapsed, "success", "", os.path.basename(attempt_out))

            last_out_decoded = f"Primary: {last_out_decoded}\nFallback(method): {fb_out_decoded}"
            logging.warning("Fallback ACVTool instrumentation (method) failed for %s (returncode=%s).", filename, fb_proc.returncode)

        except subprocess.TimeoutExpired:
            logging.error("Fallback ACVTool instrumentation (method) for %s timed out after 700 seconds. Attempting to terminate process group.", filename)
            if fb_proc:
                try:
                    _kill_process_group(fb_proc)
                except Exception:
                    pass
                try:
                    fb_out, _ = fb_proc.communicate(timeout=5)
                except Exception:
                    fb_out = None
                try:
                    os.killpg(fb_proc.pid, signal.SIGKILL)
                except Exception:
                    pass
                fb_out_decoded = fb_out.decode(errors='ignore') if fb_out else ""
                last_out_decoded = f"Primary: {last_out_decoded}\nFallbackTimeout(method): {fb_out_decoded}"
                logging.warning("Fallback(method) attempt for %s timed out.", filename)

        except Exception as e:
            try:
                fb_out, _ = fb_proc.communicate(timeout=1)
                fb_out_decoded = fb_out.decode(errors='ignore') if fb_out else ""
            except Exception:
                fb_out_decoded = ""
            last_out_decoded = f"Primary: {last_out_decoded}\nFallbackException(method): {e} {fb_out_decoded}"
            logging.exception("Fallback ACVTool instrumentation (method) raised exception for %s", filename)

        # Class-level fallback
        logging.info("Attempting fallback ACVTool instrumentation using class group for %s", filename)
        cls_cmd = [acv_executable, "instrument", "-g", "class", "-f", apk_path, "--wd", attempt_out]
        cls_start = time.time()
        cls_proc = None
        try:
            cls_proc = subprocess.Popen(cls_cmd,
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT,
                                        start_new_session=True,
                                        cwd="/tmp/")
            cls_out, _ = cls_proc.communicate(timeout=700)
            cls_elapsed = round(time.time() - cls_start, 2)
            cls_out_decoded = cls_out.decode(errors='ignore') if cls_out else ""

            if cls_proc.returncode == 0:
                logging.info("Fallback class instrumentation succeeded for %s", filename)
                try:
                    if backup_path and os.path.exists(backup_path):
                        os.remove(backup_path)
                except Exception:
                    pass
                return (filename, cls_elapsed, "success", "", os.path.basename(attempt_out))

            last_out_decoded = f"{last_out_decoded}\nFallback(class): {cls_out_decoded}"
            logging.warning("Fallback ACVTool instrumentation (class) failed for %s (returncode=%s).", filename, cls_proc.returncode)

        except subprocess.TimeoutExpired:
            logging.error("Fallback ACVTool instrumentation (class) for %s timed out after 700 seconds. Attempting to terminate process group.", filename)
            if cls_proc:
                try:
                    _kill_process_group(cls_proc)
                except Exception:
                    pass
                try:
                    cls_out, _ = cls_proc.communicate(timeout=5)
                except Exception:
                    cls_out = None
                try:
                    os.killpg(cls_proc.pid, signal.SIGKILL)
                except Exception:
                    pass
                cls_out_decoded = cls_out.decode(errors='ignore') if cls_out else ""
                last_out_decoded = f"{last_out_decoded}\nFallbackTimeout(class): {cls_out_decoded}"
                logging.warning("Fallback(class) attempt for %s timed out.", filename)

        except Exception as e:
            try:
                cls_out, _ = cls_proc.communicate(timeout=1)
                cls_out_decoded = cls_out.decode(errors='ignore') if cls_out else ""
            except Exception:
                cls_out_decoded = ""
            last_out_decoded = f"{last_out_decoded}\nFallbackException(class): {e} {cls_out_decoded}"
            logging.exception("Fallback ACVTool instrumentation (class) raised exception for %s", filename)

        # final failure after all approaches
        elapsed = round((time.time() - start) if start else 0.0, 2)
        # Restore APK from backup if present
        try:
            if backup_path and os.path.exists(backup_path):
                shutil.copy2(backup_path, apk_path)
                logging.info("Restored APK %s from backup %s after final failure", apk_path, backup_path)
                try:
                    os.remove(backup_path)
                except Exception:
                    pass
        except Exception as e:
            logging.error("Failed to restore APK from backup after final failure: %s", e)

        return (filename, elapsed, "failed", last_out_decoded, os.path.basename(attempt_out))

    except Exception as e:
        elapsed = round((time.time() - start) if start else 0.0, 2)
        out_decoded = ""
        if proc:
            try:
                out, _ = proc.communicate(timeout=1)
                out_decoded = out.decode(errors='ignore') if out else ""
            except Exception:
                pass
        return (filename, elapsed, "failed", f"{str(e)} {out_decoded}",
                os.path.basename(out_folder) if out_folder else "")

    finally:
        try:
            os.chdir(current_cwd)
        except Exception:
            pass


def _kill_process_group(proc):
    """Attempt to terminate the whole process group safely."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        pass


def _resolve_acv_executable():
    """Find the ACV executable path."""
    venv_acv = os.path.join(os.getcwd(), "venv", "bin", "acv")
    return shutil.which("acv") or (venv_acv if os.path.exists(venv_acv) else None)


def _get_target_apks(target_directory_path):
    """Scan and filter target APK files based on configuration keywords."""
    apk_path_list = glob.glob(os.path.join(target_directory_path, "**", "*.apk"), recursive=True)

    try:
        skip_keywords = POST_INJECTOR_CONFIG.get("ACVTOOL_SKIP_APK_KEYWORDS", []) or []
        skip_keywords = [k.lower() for k in skip_keywords if isinstance(k, str) and k.strip()]
    except Exception:
        skip_keywords = []

    if not skip_keywords:
        return apk_path_list

    skipped, filtered = [], []
    for p in apk_path_list:
        if any(kw in os.path.basename(p).lower() for kw in skip_keywords):
            skipped.append(p)
        else:
            filtered.append(p)

    logging.info(f"ACVTool instrumentation: skipped {len(skipped)} APK(s) matching skip keywords ({skip_keywords}).")
    if skipped:
        logging.info(f"Skipped APKs: {skipped}")

    return filtered


def _replace_original_apk_with_instrumented(apk_path, out_dirname, firmware_folder_abs, filename):
    """Attempt to replace the original APK with the instrumented version.

    Returns:
        list: A list containing the absolute path of the replaced APK, or an empty list if it failed.
    """
    result = []
    base_dir = out_dirname or os.path.basename(os.path.dirname(apk_path))
    out_folder = os.path.join(firmware_folder_abs, base_dir)
    instr_name = f"instr_{filename}"
    instr_path = os.path.join(out_folder, instr_name)
    is_valid_target = True

    logging.info(f"Attempt to replace original APK {apk_path} with instrumented APK {instr_path}")

    # Fallback logic if the exact name isn't found
    if not os.path.exists(instr_path):
        try:
            apk_files = [f for f in os.listdir(out_folder) if f.lower().endswith('.apk')]
        except Exception:
            apk_files = []

        if len(apk_files) == 1:
            instr_path = os.path.join(out_folder, apk_files[0])
            logging.info(f"Found single APK in ACV output folder {out_folder}; using {instr_path} as instrumented APK")
        elif len(apk_files) > 1:
            logging.error(f"Multiple APKs found in ACV output folder {out_folder}; cannot pick. Files: {apk_files}")
            is_valid_target = False
        else:
            is_valid_target = False

    # Attempt copy if we found a valid target
    if is_valid_target:
        if instr_path and os.path.exists(instr_path):
            try:
                shutil.copy2(instr_path, apk_path)
                logging.info(f"Replaced original APK {apk_path} with instrumented APK {instr_path}")
                # Best-effort cleanup
                try:
                    os.remove(instr_path)
                except Exception:
                    logging.warning(f"Could not remove instrumented APK {instr_path} after replacing original")

                result = [os.path.abspath(apk_path)]
            except Exception as e:
                logging.exception(f"Failed to replace original APK {apk_path} with instrumented APK {instr_path}: {e}")
        else:
            logging.error(f"Instrumented APK not found at expected location: {os.path.join(out_folder, instr_name)}")

    return result


def _clean_firmware_folder_for_archive(firmware_folder, delete_instrumented_apks):
    """Strip unnecessary directories and apks from the output folder before zipping."""
    if delete_instrumented_apks:
        removed_count = 0
        for root, _, files in os.walk(firmware_folder):
            for fname in files:
                if fname.lower().endswith('.apk'):
                    fpath = os.path.join(root, fname)
                    try:
                        os.remove(fpath)
                        removed_count += 1
                        # Create empty text file as placeholder
                        txt_fpath = os.path.join(root, os.path.splitext(fname.replace("instr_", ""))[0] + ".txt")
                        with open(txt_fpath, "w", encoding="utf-8") as f:
                            pass
                    except Exception as e:
                        logging.exception(f"Failed to remove instrumented apk {fpath}: {e}")
        logging.info(f"Removed {removed_count} instrumented APK(s) from ACV output folder before archiving")

    try:
        apktool_removed = 0
        for root, dirs, _ in os.walk(firmware_folder, topdown=False):
            for d in dirs:
                if d.lower() == 'apktool':
                    full_dpath = os.path.join(root, d)
                    try:
                        shutil.rmtree(full_dpath)
                        apktool_removed += 1
                    except Exception as e:
                        logging.debug(f"Failed to remove apktool with shutil. Using rm -rf. directory {full_dpath}: {e}")
                        try:
                            subprocess.run(['rm', '-rf', full_dpath], check=True)
                            logging.info(f"Remove apktool folder: {full_dpath}")
                        except subprocess.CalledProcessError as e:
                            logging.error(f"Failed to remove apktool. Error: {e}")
        if apktool_removed:
            logging.info(f"Removed {apktool_removed} apktool directory(ies) from ACV output folder before archiving")
    except Exception as e:
        logging.exception(f"Error while scanning for apktool directories in {firmware_folder}: {e}")


def _create_and_upload_archive(firmware_id, firmware_folder, base_path_acv, version, lunch_target, tag,
                               delete_instrumented_apks):
    """Handles zipping the contents and uploading to the repository."""
    _clean_firmware_folder_for_archive(firmware_folder, delete_instrumented_apks)

    # Format tags and filenames
    cleaned_tag = re.sub(r'\W+', '_', tag) if tag else ""
    tag_part = f"_{cleaned_tag}" if cleaned_tag else ""
    emulator_filename = f"{firmware_id}_v{version or ''}_{lunch_target or ''}{tag_part}.zip".replace('-', '_')
    archive_base = os.path.join(base_path_acv, f"acvtool_{emulator_filename}".replace('.zip', ''))

    logging.info(f"Creating ACVTool archive {archive_base}.zip from folder: {firmware_folder}")
    archive_path = None
    # Create the zip from a stable copy and verify integrity locally before upload.
    try:
        max_create_attempts = 3
        for attempt in range(1, max_create_attempts + 1):
            logging.info("Archive creation attempt %d/%d", attempt, max_create_attempts)
            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    copy_dir = os.path.join(tmpdir, 'acv_copy')
                    # Copy firmware folder to temporary location to avoid concurrent modifications
                    shutil.copytree(firmware_folder, copy_dir)
                    archive_path = shutil.make_archive(archive_base, 'zip', root_dir=copy_dir)
            except Exception as e:
                logging.warning(f"Attempt {attempt} failed during make_archive/copy: {e}")
                archive_path = None

            # verify archive exists, non-zero, valid zip, and contains members; also run CRC check via testzip
            try:
                if (archive_path and os.path.isfile(archive_path) and os.path.getsize(archive_path) > 0
                        and zipfile.is_zipfile(archive_path)):
                    with zipfile.ZipFile(archive_path, 'r') as z:
                        bad = z.testzip()
                        if bad:
                            logging.warning("Archive CRC check failed; bad member: %s", bad)
                            archive_path = None
                            # remove bad archive to avoid accidental upload
                            try:
                                os.remove(archive_path)
                            except Exception:
                                pass
                            continue
                        if not z.namelist():
                            logging.warning("Archive contains no files (attempt %d): %s", attempt, archive_path)
                            archive_path = None
                            try:
                                os.remove(archive_path)
                            except Exception:
                                pass
                            continue

                    # compute sha256 checksum for logging / local verification
                    h = hashlib.sha256()
                    with open(archive_path, 'rb') as f:
                        for chunk in iter(lambda: f.read(64 * 1024), b''):
                            h.update(chunk)
                    local_sha256 = h.hexdigest()
                    logging.info("Created and verified archive: %s (sha256=%s, size=%d)", archive_path, local_sha256, os.path.getsize(archive_path))
                    break
                else:
                    logging.warning("Archive verification failed (attempt %d): %s", attempt, archive_path)
            except Exception as e:
                logging.warning("Archive verification exception (attempt %d): %s", attempt, e)
                archive_path = None

            if attempt < max_create_attempts:
                logging.info("Retrying archive creation...")
                time.sleep(1)
            else:
                logging.error("Failed to create a valid archive for firmware %s after %d attempts", firmware_id, max_create_attempts)
                return

    except Exception as e:
        logging.error(f"Failed to create ACVTool archive for firmware {firmware_id}: {e}")
        return

    if not archive_path:
        logging.error(f"Failed to create ACVTool archive for firmware {firmware_id}")
        return

    # Upload process
    repo_base = globals().get('DOCKER_REPO_URL') or os.getenv('DOCKER_REPO_URL')
    repo_user = globals().get('DOCKER_REPO_USERNAME') or os.getenv('DOCKER_REPO_USERNAME')
    repo_pass = globals().get('DOCKER_REPO_PASSWORD') or os.getenv('DOCKER_REPO_PASSWORD')
    is_uploaded = False
    download_url = None

    if not repo_pass or not repo_user:
        error_msg = f"Repository credentials not fully provided (user: {repo_user}, pass: {'***' if repo_pass else 'None'}). Skipping upload."
        logging.error(error_msg)
        raise RuntimeError(error_msg)
    elif repo_base:
        try:
            tmp = repo_base if '://' in repo_base else f"https://{repo_base}"
            parsed = urlparse(tmp)
            domain_base = f"{parsed.scheme or 'https'}://{parsed.netloc}" if parsed.netloc else repo_base.rstrip('/')
            raw_repo = f"{domain_base.rstrip('/')}/repository/raw_files/"
        except Exception:
            raw_repo = f"{repo_base.rstrip('/')}/raw_files/"

        archive_filename = os.path.basename(archive_path)
        logging.info(f"Uploading ACVTool archive {archive_filename} to raw_files repository {raw_repo}")

        is_uploaded = False
        download_url = None
        local_sha256 = ""
        try:
            is_uploaded, download_url = upload_build_artefact(raw_repo, repo_user, repo_pass, archive_path,
                                                              archive_filename)
            if is_uploaded:
                logging.info(f"ACVTool archive uploaded successfully: {download_url}")
                # Persist checksum locally for traceability
                try:
                    checksum_file = f"{archive_path}.sha256"
                    with open(checksum_file, 'w', encoding='utf-8') as cf:
                        cf.write(f"{local_sha256}  {archive_filename}\n")
                    logging.info("Wrote local archive checksum to %s", checksum_file)
                except Exception as e:
                    logging.warning("Failed to write local checksum file: %s", e)
            else:
                logging.error("Failed to upload ACVTool archive to raw_files repository")
        except Exception as e:
            logging.exception(f"Error while uploading ACVTool archive to raw_files: {e}")
            raise e
    else:
        logging.error("No repository base provided; skipping upload of ACVTool archive")

    # Cleanup intermediate output folder and archive only if upload succeeded
    if is_uploaded:
        if archive_path and os.path.exists(archive_path):
            try:
                shutil.rmtree(firmware_folder)
                logging.info(f"Removed intermediate ACVTool folder after archiving: {firmware_folder}")
                try:
                    os.remove(archive_path)
                    logging.info(f"Removed ACV Archive: {archive_path}")
                except Exception as e:
                    logging.warning(f"Failed to remove archive file {archive_path}: {e}")
            except Exception as e:
                logging.warning(f"Failed to remove intermediate ACVTool folder {firmware_folder}: {e}")
    else:
        logging.error("Upload did not succeed; leaving archive and firmware folder for inspection.")
        raise RuntimeError(f"Failed to upload ACVTool archive to raw_files: {archive_path}")


def add_acvtool_instrumentation_multiprocessing(firmware_id,
                                                aosp_path,
                                                cookies,
                                                target_directory_path,
                                                version=None,
                                                lunch_target=None,
                                                tag=None,
                                                delete_instrumented_apks=False,
                                                max_workers=None,
                                                post_injector_config=None,
                                                upload_data=False
                                                ):
    """
    Parallel version of add_acvtool_instrumentation using multiple processes.
    Processes APKs in parallel using a process pool. Writes a timing JSON (same layout as
    the single-process function) into the firmware folder under BUILD_OUT_PATH.
    """
    global POST_INJECTOR_CONFIG
    POST_INJECTOR_CONFIG = post_injector_config
    result_dict = {"success": [], "failed": []}
    acv_executable = _resolve_acv_executable()

    if acv_executable is None:
        logging.error(
            "ACVTool `acv` not found in PATH and no local `venv/bin/acv` found. Skipping ACVTool instrumentation.")
        return result_dict

    apk_path_list = _get_target_apks(target_directory_path)
    logging.info(f"Found {len(apk_path_list)} APK files for ACVTool instrumentation (parallel mode).")

    partition_name = str(os.path.basename(target_directory_path))
    base_path_acv = str(os.path.join(BUILD_OUT_PATH, "acvtool_instrumentation"))
    firmware_folder = str(os.path.join(base_path_acv, firmware_id))
    partition_path = str(os.path.join(base_path_acv, firmware_id, partition_name))
    shutil.rmtree(partition_path, ignore_errors=True)
    os.makedirs(firmware_folder, exist_ok=True)
    os.makedirs(partition_path, exist_ok=True)
    logging.info(f"Deleted and recreated ACVTool instrumentation folder: {partition_path}")

    subfolder_abs = os.path.abspath(partition_path)
    worker_args = [(apk, firmware_folder, acv_executable, subfolder_abs) for apk in apk_path_list]

    max_workers = min(len(apk_path_list) or 1, max(1, os.cpu_count() * 4 if os.cpu_count() else 4))

    logging.info(f"Starting instrumentation with {max_workers} worker(s)")

    per_file_times = {}
    acv_error_entries = []
    start_time = time.time()
    inst_apk_path_list = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_acv_instrument_worker, args): args[0] for args in worker_args}

        for fut in concurrent.futures.as_completed(futures):
            apk = futures[fut]
            try:
                # Resolve true apk path if missing or stripped
                if not apk or not os.path.isabs(apk) or not os.path.exists(apk):
                    basename = os.path.basename(str(apk))
                    matches = [p for p in apk_path_list if os.path.basename(p) == basename]
                    apk = matches[0] if matches else futures[fut]

                filename, elapsed, status, error, out_dirname = fut.result()
                per_file_times[filename] = {"duration_seconds": elapsed, "status": status}

                if status == "success":
                    result_dict["success"].append(filename)
                    logging.info(f"ACVTool instrumentation succeeded for {filename} in {elapsed}s (parallel)")
                    inst_apk_path_list = _replace_original_apk_with_instrumented(apk, out_dirname, subfolder_abs, filename)
                else:
                    result_dict["failed"].append(filename)
                    acv_error_entries.append({
                        "filename": filename,
                        "duration_seconds": elapsed,
                        "error": error,
                    })
                    logging.error(f"ACVTool instrumentation failed for {filename} ({apk}) in {elapsed}s (parallel)")
                    logging.debug(f"ACVTool error: {error}")
            except Exception as e:
                fname = os.path.basename(str(futures[fut]))
                per_file_times[fname] = {"duration_seconds": 0.0, "status": "failed", "error": str(e)}
                result_dict["failed"].append(fname)
                logging.exception(f"Worker for {fname} failed unexpectedly: {e}")

    end_time = time.time()
    total_duration = round(end_time - start_time, 2)

    logging.info(f"ACVTool instrumentation completed in {total_duration}s with Instrumented Apks: {len(inst_apk_path_list)} "
                 f"Successes: {len(result_dict['success'])}, "
                 f"Failures: {len(result_dict['failed'])}. ")
    for apk_path in inst_apk_path_list:
        error = handle_app_modules(apk_path, aosp_path, firmware_id, cookies)
        logging.error(f"ACVTool instrumentation signing APK failed: {error}")

    summary = {
        "hostname": os.uname()[1],
        "firmware_id": firmware_id,
        "start_time": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_time)),
        "end_time": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(end_time)),
        "acv_instrumentation_duration_seconds": total_duration,
        "per_file_durations": per_file_times,
        "result": result_dict,
    }

    try:
        logging.info(f"Writing ACVTool instrumentation result: {summary}")
        write_json_output(summary, PATH_BUILD_ACV_LOG)
    except Exception as err:
        logging.error(f"Failed to write timing JSON: {err}")

    if acv_error_entries:
        try:
            write_json_nd_output(acv_error_entries, PATH_BUILD_ACV_ERROR_LOG)
            logging.info(f"Wrote ACVTool errors to {PATH_BUILD_ACV_ERROR_LOG} (entries={len(acv_error_entries)})")
        except Exception as err:
            logging.exception(f"Failed to write ACVTool error log: {err}")

    logging.info(f"ACVTool instrumentation parallel result: {result_dict}")

    if upload_data:
        tag = f"{tag}_{partition_name}"
        _create_and_upload_archive(
            firmware_id,
            partition_path,
            base_path_acv,
            version,
            lunch_target,
            tag,
            delete_instrumented_apks
        )
    result_len = {"success": len(result_dict["success"]), "failed": len(result_dict['failed'])}
    return result_len
