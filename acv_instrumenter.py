import concurrent
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
from pathlib import Path
from urllib.parse import urlparse

from common import upload_build_artefact
from config import (
    PATH_BUILD_ACV_ERROR_LOG,
    PATH_BUILD_ACV_LOG,
    PRE_INJECTOR_CONFIG,
    BUILD_OUT_PATH
)
from json_writer import write_json_output, write_json_nd_output


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

        cmd = [acv_executable, "instrument", "-f", apk_path, "--wd", out_folder]
        start = time.time()

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)

        try:
            out, _ = proc.communicate(timeout=700)
            elapsed = round(time.time() - start, 2)
            out_decoded = out.decode(errors='ignore') if out else ""

            if proc.returncode != 0:
                return (filename, elapsed, "failed", out_decoded, os.path.basename(out_folder))
            return (filename, elapsed, "success", "", os.path.basename(out_folder))

        except subprocess.TimeoutExpired:
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
            return (filename, elapsed, "failed", f"TimeoutExpired: {out_decoded}", os.path.basename(out_folder))

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
        skip_keywords = PRE_INJECTOR_CONFIG.get("ACVTOOL_SKIP_APK_KEYWORDS", []) or []
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
        logging.debug(f"Skipped APKs: {skipped}")

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
                        logging.exception(f"Failed to remove apktool directory {full_dpath}: {e}")

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
    try:
        archive_path = shutil.make_archive(archive_base, 'zip', root_dir=firmware_folder)
    except Exception as e:
        logging.error(f"Failed to create ACVTool archive for firmware {firmware_id}: {e}")
        return

    # Upload process
    repo_base = globals().get('DOCKER_REPO_URL_GLOBAL')
    repo_user = globals().get('DOCKER_REPO_USERNAME_GLOBAL')
    repo_pass = globals().get('DOCKER_REPO_PASSWORD_GLOBAL')

    if not repo_pass or not repo_user:
        logging.error(
            f"Repository credentials not fully provided (user: {repo_user}, pass: {'***' if repo_pass else 'None'}). Skipping upload.")
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

        try:
            is_uploaded, download_url = upload_build_artefact(raw_repo, repo_user, repo_pass, archive_path,
                                                              archive_filename)
            if is_uploaded:
                logging.info(f"ACVTool archive uploaded successfully: {download_url}")
            else:
                logging.error("Failed to upload ACVTool archive to raw_files repository")
        except Exception as e:
            logging.exception(f"Error while uploading ACVTool archive to raw_files: {e}")
            raise e
    else:
        logging.debug("No repository base provided; skipping upload of ACVTool archive")

    # Cleanup intermediate output folder on success
    if archive_path and os.path.exists(archive_path):
        try:
            shutil.rmtree(firmware_folder)
            logging.info(f"Removed intermediate ACVTool folder after archiving: {firmware_folder}")
            os.remove(archive_path)
            logging.info(f"Removed ACV Archive: {archive_path}")
        except Exception as e:
            logging.warning(f"Failed to remove intermediate ACVTool folder {firmware_folder}: {e}")


def add_acvtool_instrumentation_multiprocessing(firmware_id,
                                                aosp_path,
                                                cookies,
                                                target_directory_path,
                                                version=None,
                                                lunch_target=None,
                                                tag=None,
                                                delete_instrumented_apks=False,
                                                max_workers=None
                                                ):
    """
    Parallel version of add_acvtool_instrumentation using multiple processes.
    Processes APKs in parallel using a process pool. Writes a timing JSON (same layout as
    the single-process function) into the firmware folder under BUILD_OUT_PATH.
    """
    result_dict = {"success": [], "failed": []}
    acv_executable = _resolve_acv_executable()

    if acv_executable is None:
        logging.error(
            "ACVTool `acv` not found in PATH and no local `venv/bin/acv` found. Skipping ACVTool instrumentation.")
        return result_dict

    apk_path_list = _get_target_apks(target_directory_path)
    logging.info(f"Found {len(apk_path_list)} APK files for ACVTool instrumentation (parallel mode).")

    base_path_acv = str(os.path.join(BUILD_OUT_PATH, "acvtool_instrumentation"))
    firmware_folder = str(os.path.join(base_path_acv, firmware_id))
    shutil.rmtree(firmware_folder, ignore_errors=True)
    os.makedirs(firmware_folder, exist_ok=True)
    logging.info(f"Deleted and recreated ACVTool instrumentation folder: {firmware_folder}")

    firmware_folder_abs = os.path.abspath(firmware_folder)
    worker_args = [(apk, firmware_folder, acv_executable, firmware_folder_abs) for apk in apk_path_list]

    max_workers = min(len(apk_path_list) or 1, max(1, os.cpu_count() * 10 if os.cpu_count() else 4))

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
                    inst_apk_path_list = _replace_original_apk_with_instrumented(apk, out_dirname, firmware_folder_abs, filename)
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

    _create_and_upload_archive(
        firmware_id,
        firmware_folder,
        base_path_acv,
        version,
        lunch_target,
        tag,
        delete_instrumented_apks
    )
    result_len = {"success": len(result_dict["success"]), "failed": len(result_dict['failed'])}
    return result_len













#
#
# def _acv_instrument_worker(params):
#     """Worker called in a separate process to instrument a single APK.
#
#     params: tuple(apk_path, firmware_folder, acv_executable, safe_cwd)
#     returns: tuple(filename, elapsed, status, error_message)
#     """
#     apk_path, firmware_folder, acv_executable, safe_cwd = params
#     filename = os.path.basename(apk_path)
#     current_cwd = os.path.abspath(os.getcwd())
#     start = None
#     proc = None
#     try:
#         # create a unique out folder under firmware_folder using parent dir name
#         # If the folder already exists, append a suffix ("_1", "_2", ...) until a non-existing folder is found.
#         base_dir = Path(apk_path).parent.name
#         out_folder = os.path.join(firmware_folder, base_dir)
#         if os.path.exists(out_folder):
#             idx = 1
#             while True:
#                 candidate_name = f"{base_dir}_{idx}"
#                 candidate_path = os.path.join(firmware_folder, candidate_name)
#                 if not os.path.exists(candidate_path):
#                     out_folder = candidate_path
#                     break
#                 idx += 1
#         # create the (unique) out folder
#         os.makedirs(out_folder, exist_ok=True)
#         os.chdir(safe_cwd)
#         cmd = [acv_executable, "instrument", "-f", apk_path, "--wd", out_folder]
#         start = time.time()
#         # start process in a new session / process group so we can kill the whole group on timeout
#         proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
#         try:
#             out, _ = proc.communicate(timeout=700)
#             elapsed = round(time.time() - start, 2)
#             if proc.returncode != 0:
#                 out_decoded = out.decode(errors='ignore') if out else ""
#                 return (filename, elapsed, "failed", out_decoded, os.path.basename(out_folder))
#             return (filename, elapsed, "success", "", os.path.basename(out_folder))
#         except subprocess.TimeoutExpired:
#             # Attempt to terminate the whole process group first, then force kill if necessary
#             elapsed = round(time.time() - start, 2)
#             try:
#                 os.killpg(proc.pid, signal.SIGTERM)
#             except Exception:
#                 pass
#             # give processes a short grace period to exit and collect output
#             try:
#                 out, _ = proc.communicate(timeout=5)
#             except Exception:
#                 out = None
#             # ensure everything is dead
#             try:
#                 os.killpg(proc.pid, signal.SIGKILL)
#             except Exception:
#                 pass
#             out_decoded = out.decode(errors='ignore') if out else ""
#             return (filename, elapsed, "failed", f"TimeoutExpired: {out_decoded}", os.path.basename(out_folder))
#     except Exception as e:
#         elapsed = round((time.time() - start) if start else 0.0, 2)
#         # try to capture any remaining output
#         out = None
#         try:
#             if proc:
#                 out, _ = proc.communicate(timeout=1)
#         except Exception:
#             pass
#         out_decoded = out.decode(errors='ignore') if out else ""
#         return (filename, elapsed, "failed", f"{str(e)} {out_decoded}", os.path.basename(out_folder) if 'out_folder' in locals() else "")
#     finally:
#         try:
#             os.chdir(current_cwd)
#         except Exception:
#             pass
#
#
# def add_acvtool_instrumentation_multiprocessing(firmware_id, version=None, lunch_target=None, tag=None, delete_instrumented_apks=False, max_workers=None):
#     """Parallel version of add_acvtool_instrumentation using multiple processes.
#
#     Processes APKs in parallel using a process pool. Writes a timing JSON (same layout as
#     the single-process function) into the firmware folder under BUILD_OUT_PATH.
#
#     :param firmware_id: str - identifier used to create the firmware folder
#     :param max_workers: int|None - number of worker processes. If None, defaults to os.cpu_count().
#     :returns: dict with lists of successes and failures (same shape as existing function)
#     """
#     result_dict = {"success": [], "failed": []}
#
#     # Resolve acv
#     venv_acv = os.path.join(os.getcwd(), "venv", "bin", "acv")
#     acv_executable = shutil.which("acv") or (venv_acv if os.path.exists(venv_acv) else None)
#     if acv_executable is None:
#         logging.error("ACVTool `acv` not found in PATH and no local `venv/bin/acv` found. Skipping ACVTool instrumentation.")
#         return result_dict
#
#     apk_path_list = glob.glob(os.path.join(EXTRACTED_PACKAGES_PATH, "**", "*.apk"), recursive=True)
#     # Allow pre-injector configuration to specify keywords which, when present in an APK filename,
#     # cause the APK to be skipped for ACVTool instrumentation.
#     skip_keywords = []
#     try:
#         skip_keywords = PRE_INJECTOR_CONFIG.get("ACVTOOL_SKIP_APK_KEYWORDS", []) or []
#         # normalize to lowercase for case-insensitive matching
#         skip_keywords = [k.lower() for k in skip_keywords if isinstance(k, str) and k.strip()]
#     except Exception:
#         skip_keywords = []
#
#     if skip_keywords:
#         initial_count = len(apk_path_list)
#         skipped_list = []
#         filtered = []
#         for p in apk_path_list:
#             name = os.path.basename(p).lower()
#             if any(kw in name for kw in skip_keywords):
#                 skipped_list.append(p)
#             else:
#                 filtered.append(p)
#         apk_path_list = filtered
#         logging.info(f"ACVTool instrumentation: skipped {len(skipped_list)} APK(s) matching skip keywords ({skip_keywords}).")
#         if skipped_list:
#             logging.debug(f"Skipped APKs: {skipped_list}")
#
#     logging.info(f"Found {len(apk_path_list)} APK files for ACVTool instrumentation (parallel mode).")
#
#     per_file_times = {}
#     acv_error_entries = []
#     start_time = time.time()
#
#     base_path_acv = str(os.path.join(BUILD_OUT_PATH, "acvtool_instrumentation"))
#     firmware_folder = str(os.path.join(base_path_acv, firmware_id))
#     shutil.rmtree(firmware_folder, ignore_errors=True)
#     os.makedirs(firmware_folder, exist_ok=True)
#     logging.info(f"Deleted and recreated ACVTool instrumentation folder: {firmware_folder}")
#
#     firmware_folder_abs = os.path.abspath(firmware_folder)
#     worker_args = [(apk_path, firmware_folder, acv_executable, firmware_folder_abs) for apk_path in apk_path_list]
#
#     if max_workers is None:
#         try:
#             max_workers = os.cpu_count() * 3 or 4
#         except Exception:
#             max_workers = 4
#
#     logging.info(f"Starting instrumentation with {max_workers} worker(s)")
#
#     # run in parallel
#     with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
#         futures = {executor.submit(_acv_instrument_worker, args): args[0] for args in worker_args}
#         for fut in concurrent.futures.as_completed(futures):
#             # `futures` maps each Future -> original apk path (or sometimes just a filename).
#             # Ensure we work with the original absolute path. If only a filename was stored,
#             # try to resolve it from the apk_path_list gathered earlier.
#             apk = futures[fut]
#             try:
#                 # if apk is not an absolute path or doesn't exist on disk, attempt to find
#                 # the original full path by matching the basename against the discovered list
#                 if not apk or (not os.path.isabs(apk) or not os.path.exists(apk)):
#                     basename = os.path.basename(str(apk))
#                     matches = [p for p in apk_path_list if os.path.basename(p) == basename]
#                     if matches:
#                         apk = matches[0]
#                     else:
#                         apk = futures[fut]
#                 # _acv_instrument_worker returns (filename, elapsed, status, error, out_folder_basename)
#                 filename, elapsed, status, error, out_dirname = fut.result()
#                 per_file_times[filename] = {"duration_seconds": elapsed, "status": status}
#                 if status == "success":
#                     result_dict["success"].append(filename)
#                     logging.info(f"ACVTool instrumentation succeeded for {filename} in {elapsed}s (parallel)")
#                     # If instrumentation succeeded, replace the original APK with the instrumented one
#                     try:
#                         # original apk path is in `apk` (from futures mapping)
#                         # Use the actual output folder basename returned by the worker. Fall back to the
#                         # original parent folder name if, for some reason, the worker didn't provide it.
#                         base_dir = out_dirname or os.path.basename(os.path.dirname(apk))
#                         out_folder = os.path.join(firmware_folder_abs, base_dir)
#                         instr_name = f"instr_{filename}"
#                         instr_path = os.path.join(out_folder, instr_name)
#                         logging.info(f"Attempt to replace original APK %s with instrumented APK %s", apk, instr_path)
#                         try:
#                             if not os.path.exists(instr_path):
#                                 # Fallback: search for any .apk file in the ACV output folder
#                                 try:
#                                     apk_files = [f for f in os.listdir(out_folder) if f.lower().endswith('.apk')]
#                                 except Exception:
#                                     apk_files = []
#                                 if len(apk_files) == 1:
#                                     found = os.path.join(out_folder, apk_files[0])
#                                     logging.info('Found single APK in ACV output folder %s; using %s as instrumented APK', out_folder, found)
#                                     instr_path = found
#                                 elif len(apk_files) > 1:
#                                     logging.error('Multiple APKs found in ACV output folder %s; cannot deterministically pick instrumented APK. Files: %s', out_folder, apk_files)
#                                     instr_path = None
#                                 else:
#                                     instr_path = None
#
#                             if instr_path and os.path.exists(instr_path):
#                                 try:
#                                     # Copy the discovered instrumented APK over the original APK path (preserving original name)
#                                     shutil.copy2(instr_path, apk)
#                                     logging.info('Replaced original APK %s with instrumented APK %s', apk, instr_path)
#                                     # Attempt to remove the instrumented file in the ACV output (best-effort)
#                                     try:
#                                         os.remove(instr_path)
#                                     except Exception:
#                                         logging.warning('Could not remove instrumented APK %s after replacing original', instr_path)
#                                 except Exception:
#                                     logging.exception('Failed to replace original APK %s with instrumented APK %s', apk, instr_path)
#                             else:
#                                 logging.error('Instrumented APK not found at expected location: %s', os.path.join(out_folder, instr_name))
#                         except Exception:
#                             logging.exception('Unexpected error while attempting to replace original APK %s with instrumented APK in %s', apk, out_folder)
#                     except Exception:
#                         logging.exception('Unexpected error while attempting to replace original APK for %s', filename)
#                 else:
#                     result_dict["failed"].append(filename)
#                     # Record error separately (do not include error strings inside the results_acv.json)
#                     acv_error_entries.append({
#                         "filename": filename,
#                         "duration_seconds": elapsed,
#                         "error": error,
#                     })
#                     logging.error(f"ACVTool instrumentation failed for {filename} ({apk}) in {elapsed}s (parallel)")
#                     logging.debug(f"ACVTool error: {error}")
#             except Exception as e:
#                 # Shouldn't happen often; record generic failure
#                 fname = os.path.basename(futures[fut])
#                 per_file_times[fname] = {"duration_seconds": 0.0, "status": "failed", "error": str(e)}
#                 result_dict["failed"].append(fname)
#                 logging.exception(f"Worker for {fname} failed unexpectedly: {e}")
#
#     end_time = time.time()
#     total_duration = round(end_time - start_time, 2)
#
#     summary = {
#         "hostname": os.uname()[1],
#         "firmware_id": firmware_id,
#         "start_time": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_time)),
#         "end_time": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(end_time)),
#         "acv_instrumentation_duration_seconds": total_duration,
#         "per_file_durations": per_file_times,
#         "result": result_dict,
#     }
#
#     # write timing JSON to firmware folder
#     try:
#         logging.info(f"Writing ACVTool instrumentation result: {summary}")
#         # Summary should not contain raw error messages; errors are written to a separate acv_error.log
#         write_json_output(summary, PATH_BUILD_ACV_LOG)
#     except Exception as err:
#         logging.error(f"Failed to write timing JSON: {err}")
#
#     # Write ACVTool errors (if any) to a separate NDJSON error log to avoid polluting results_acv.json
#     if acv_error_entries:
#         try:
#             for entry in acv_error_entries:
#                 # append each error as a JSON line
#                 from json_writer import write_json_nd_output
#                 write_json_nd_output(entry, PATH_BUILD_ACV_ERROR_LOG)
#             logging.info('Wrote ACVTool errors to %s (entries=%d)', PATH_BUILD_ACV_ERROR_LOG, len(acv_error_entries))
#         except Exception as err:
#             logging.exception('Failed to write ACVTool error log: %s', err)
#
#     logging.info(f"ACVTool instrumentation parallel result: {result_dict}")
#     # Create a single zip archive for the firmware's ACVTool output to save space and remove intermediate files.
#     try:
#         # Build archive filename to match the emulator image artefact filename, prefixed with 'acvtool_'
#         # Determine tag part similar to process_firmware_ids
#         try:
#             if tag:
#                 # sanitize tag first to avoid using backslashes inside the f-string expression
#                 sanitized_tag = re.sub(r'\W+', '_', tag)
#                 tag_part = f"_{sanitized_tag}"
#             else:
#                 tag_part = ""
#         except Exception:
#             tag_part = ""
#         # If version or lunch_target are not provided, fall back to safe defaults
#         ver = version or ''
#         lt = lunch_target or ''
#         emulator_filename = f"{firmware_id}_v{ver}_{lt}{tag_part}.zip".replace('-', '_')
#         acv_filename = f"acvtool_{emulator_filename}"
#         archive_base = os.path.join(base_path_acv, acv_filename.replace('.zip', ''))
#         # If requested, remove instrumented .apk files from the ACV output folder so they are not included in the archive
#         if delete_instrumented_apks:
#             removed_count = 0
#             for root, dirs, files in os.walk(firmware_folder):
#                 for fname in files:
#                     if fname.lower().endswith('.apk'):
#                         fpath = os.path.join(root, fname)
#                         try:
#                             os.remove(fpath)
#                             removed_count += 1
#                             fpath = os.path.join(root, os.path.splitext(fname.replace("instr_", ""))[0] + ".txt")
#                             with open(fpath, "w", encoding="utf-8") as f:
#                                 # empty file (creates/truncates)
#                                 pass
#                         except Exception:
#                             logging.exception('Failed to remove instrumented apk: %s', fpath)
#             logging.info('Removed %d instrumented APK(s) from ACV output folder before archiving', removed_count)
#         # Remove any directories named 'apktool' from the firmware_folder to avoid including build artifacts
#         try:
#             apktool_removed = 0
#             for root, dirs, files in os.walk(firmware_folder):
#                 # iterate over a copy since we may modify dirs in-place
#                 for d in list(dirs):
#                     if d.lower() == 'apktool':
#                         full_dpath = os.path.join(root, d)
#                         try:
#                             shutil.rmtree(full_dpath)
#                             apktool_removed += 1
#                             # prevent os.walk from descending into this directory
#                             dirs.remove(d)
#                         except Exception:
#                             logging.exception('Failed to remove apktool directory: %s', full_dpath)
#             if apktool_removed:
#                 logging.info('Removed %d apktool directory(ies) from ACV output folder before archiving', apktool_removed)
#         except Exception:
#             logging.exception('Error while scanning for apktool directories in %s', firmware_folder)
#         logging.info(f"Creating ACVTool archive {archive_base}.zip from folder: {firmware_folder}")
#         archive_path = shutil.make_archive(archive_base, 'zip', root_dir=firmware_folder)
#
#         # Attempt to upload the created archive to the FMD Nexus repository raw_files
#         try:
#             # Use repository base provided via globals (set at startup) or environment variables
#             repo_base = globals().get('DOCKER_REPO_URL_GLOBAL')
#             repo_user = globals().get('DOCKER_REPO_USERNAME_GLOBAL')
#             repo_pass = globals().get('DOCKER_REPO_PASSWORD_GLOBAL')
#
#             if not repo_pass or not repo_user:
#                 logging.error("Repository credentials not fully provided (user: %s, pass: %s). Skipping upload of ACVTool archive.", repo_user, '***' if repo_pass else None)
#                 raise RuntimeError("Repository credentials not fully provided")
#
#             if repo_base:
#                 # Normalize the provided repo_base by stripping any path segments and keeping scheme+domain:port
#                 try:
#                     tmp = repo_base
#                     if '://' not in tmp:
#                         tmp = 'https://' + tmp
#                     parsed = urlparse(tmp)
#                     scheme = parsed.scheme or 'https'
#                     netloc = parsed.netloc
#                     if not netloc:
#                         # Fallback: use the original string without trailing slash
#                         domain_base = repo_base.rstrip('/')
#                     else:
#                         domain_base = f"{scheme}://{netloc}"
#                     # Reconstruct repository path to point to repository/raw_files under the domain:port
#                     raw_repo = domain_base.rstrip('/') + '/repository/raw_files/'
#                 except Exception:
#                     raw_repo = repo_base.rstrip('/') + '/raw_files/'
#                 archive_filename = os.path.basename(archive_path)
#                 logging.info(f'Uploading ACVTool archive {archive_filename} to raw_files repository {raw_repo}')
#                 try:
#                     is_uploaded, download_url = upload_build_artefact(raw_repo, repo_user, repo_pass, archive_path, archive_filename)
#                     if is_uploaded:
#                         logging.info(f'ACVTool archive uploaded successfully: {download_url}')
#                     else:
#                         logging.error('Failed to upload ACVTool archive to raw_files repository')
#                 except Exception as e:
#                     logging.exception(f'Error while uploading ACVTool archive to raw_files: {e}')
#             else:
#                 logging.debug('No repository base provided; skipping upload of ACVTool archive')
#         except Exception:
#             logging.exception('Unexpected error during ACVTool archive upload step')
#
#         # If archive created successfully, remove the intermediate firmware_folder to save disk space
#         if archive_path and os.path.exists(archive_path):
#             try:
#                 shutil.rmtree(firmware_folder)
#                 logging.info(f"Removed intermediate ACVTool folder after archiving: {firmware_folder}")
#             except Exception as e:
#                 logging.warning(f"Failed to remove intermediate ACVTool folder {firmware_folder}: {e}")
#         logging.info(f"ACVTool archive created: {archive_path}")
#     except Exception as e:
#         logging.error(f"Failed to create ACVTool archive for firmware {firmware_id}: {e}")
#     return result_dict