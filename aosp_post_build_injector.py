"""
This script includes methods to inject objects into the AOSP source code after the source code has been built and
before it is packaged into a firmware image. The script is used to inject blobs into the file system to enable
the replacement of the original blobs (from AOSP) with the vendor flavoured blobs.
"""
import argparse
import glob
import hashlib
import re
import shutil
import logging
import subprocess
import tempfile
import threading
import time
import json
import os
import stat
import traceback
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor as Executor, as_completed
from http import cookies
from types import MappingProxyType
from pathlib import Path
from filelock import FileLock
from acv_instrumenter import add_acvtool_instrumentation_multiprocessing
from aosp_apex_injector import handle_apex_modules, prepare_capex, rename_file, repackage_apex_file, \
    POST_INJECTOR_CONFIG, add_new_apex_file
from aosp_build_property_merger import start_property_merge
from aosp_module_type import get_module_type
from aosp_post_injector_semantic import handle_vintf_merge, handle_seplicy_merging
from aosp_rc_merger import run_rc_merger
from common import extract_vendor_name, remove_vendor_name_from_path, load_configs, is_elf_binary, \
    check_shared_object_architecture, get_path_up_to_first_term, get_md5_from_file, check_binary_architecture, \
    get_aosp_build_out_dir
from common_post_injector import (
    handle_app_modules,
    handle_jar_module,
    ensure_archive_has_dex,
    build_aosp_apk_index,
    reconcile_and_deduplicate_vendor_apk,
    generate_privapp_permissions_allowlist,
)
from config import AOSP_BUILD_OUT_SDK_ARM64_x64_PATH_EMU64A, AOSP_BUILD_OUT_SDK_ARM64_x64_PATH, \
    MEASURE_LOOKUP_PERFORMANCE, EXTRACTION_ALL_FILES_PATH
from config_post_injector import *
try:
    from elftools.elf.elffile import ELFFile
    from elftools.elf.dynamic import DynamicSection
    from elftools.elf.sections import SymbolTableSection
except ImportError:
    ELFFile = None
    DynamicSection = None
    SymbolTableSection = None
from fmd_backend_requests import get_csrf_token, authenticate_fmd
from json_writer import write_json_nd_output, write_json_output
from setup_logger import setup_logger
from tqdm import tqdm
from fast_copy import schedule_copy, wait_for_all_copy_tasks
import concurrent.futures


if os.environ.get("FMD_DEBUG") == "True":
    setup_logger(logging.DEBUG)
else:
    setup_logger(MAIN_LOG_FILE_PATH)
    setup_logger(logger_name="semantic_injector", log_file=SEMANTIC_INJECTOR_LOG_FILE_PATH)


processed_files_lock = threading.Lock()
# Immutable mapping of md5 -> tuple(file_paths). Built once before worker tasks start.
INTERMEDIATE_MD5_MAP = None
# Cache of file indexes: target_obj_path -> { basename -> [fullpaths] }
FILE_INDEX_CACHE = {}
FILE_INDEX_LOCK = threading.Lock()
AOSP_APK_INDEX = None
AOSP_APK_INDEX_LOCK = threading.Lock()


def snapshot_aosp_core_groups(target_out_path, backup_root_dir=None):
    """
    Snapshots original AOSP files for COUPLED_CORE_GROUPS (both partition files and
    matching target_out_path/obj/ files) before post-build injection overwrites them.
    Returns { group_name: { rel_path: { "target": abs_path, "backup": backup_path, "md5": md5, "obj_backups": [(obj_path, backup_obj_path), ...] } } }.
    """
    if backup_root_dir is None:
        backup_root_dir = tempfile.mkdtemp(prefix="fmd_core_backups_")
    os.makedirs(backup_root_dir, exist_ok=True)

    snapshots = {}
    obj_root = os.path.join(target_out_path, FOLDER_NAME_OBJECTS) if target_out_path else ""
    for group_name, rel_paths in COUPLED_CORE_GROUPS.items():
        group_snap = {}
        for rel_path in rel_paths:
            abs_target = os.path.join(target_out_path, rel_path)
            if not os.path.exists(abs_target) or os.path.islink(abs_target):
                continue
            backup_path = os.path.join(backup_root_dir, group_name, rel_path)
            os.makedirs(os.path.dirname(backup_path), exist_ok=True)
            shutil.copy2(abs_target, backup_path)
            md5 = get_md5_from_file(abs_target)

            obj_backups = []
            if os.path.isdir(obj_root):
                fname = os.path.basename(rel_path)
                for root, _, files in scandir_walk(obj_root):
                    if fname in files:
                        obj_cand = os.path.join(root, fname)
                        if os.path.isfile(obj_cand) and not os.path.islink(obj_cand):
                            rel_obj = os.path.relpath(obj_cand, target_out_path)
                            b_obj = os.path.join(backup_root_dir, group_name, "_obj", rel_obj)
                            os.makedirs(os.path.dirname(b_obj), exist_ok=True)
                            shutil.copy2(obj_cand, b_obj)
                            obj_backups.append((obj_cand, b_obj))

            group_snap[rel_path] = {
                "target": abs_target,
                "backup": backup_path,
                "md5": md5,
                "obj_backups": obj_backups,
            }
        snapshots[group_name] = group_snap
    return snapshots


def _extract_elf_info(elf_path):
    """
    Returns (elfclass_bits: int, needed_libs: list[str], undef_symbols: set[str], defined_symbols: set[str])
    or None if elf_path is not a valid ELF file.
    """
    if not elf_path or not os.path.isfile(elf_path):
        return None
    try:
        with open(elf_path, "rb") as f:
            ident = f.read(16)
            if len(ident) < 5 or ident[:4] != b"\x7fELF":
                return None
            if ELFFile is None:
                bits = 64 if ident[4] == 2 else 32
                needed = []
                readelf_bin = shutil.which("readelf") or shutil.which("llvm-readelf")
                if readelf_bin:
                    res = subprocess.run([readelf_bin, "-d", "-W", elf_path], capture_output=True, text=True)
                    if res.returncode == 0:
                        for line in res.stdout.splitlines():
                            if "(NEEDED)" in line:
                                m = re.search(r"\[([^\]]+)\]", line)
                                if m:
                                    needed.append(m.group(1))
                return bits, needed, set(), set()
            f.seek(0)
            elf = ELFFile(f)
            bits = elf.elfclass
            needed = []
            undef_syms = set()
            def_syms = set()
            for sec in elf.iter_sections():
                if isinstance(sec, DynamicSection):
                    for tag in sec.iter_tags():
                        if tag.entry.d_tag == "DT_NEEDED":
                            needed.append(tag.needed)
                elif isinstance(sec, SymbolTableSection) and sec.name == ".dynsym":
                    for sym in sec.iter_symbols():
                        name = sym.name
                        if not name:
                            continue
                        bind = sym["st_info"]["bind"]
                        stype = sym["st_info"]["type"]
                        shndx = sym["st_shndx"]
                        if shndx == "SHN_UNDEF":
                            if bind == "STB_GLOBAL" and stype in ("STT_FUNC", "STT_OBJECT"):
                                undef_syms.add(name)
                        else:
                            def_syms.add(name)
            return bits, needed, undef_syms, def_syms
    except Exception as e:
        logging.debug(f"Could not parse ELF {elf_path}: {e}")
        return None


def resolve_and_validate_elf_dependencies(elf_path, source_folder_path, target_out_path):
    """
    Recursively inspects DT_NEEDED entries of elf_path using pyelftools.
    If any required DT_NEEDED library is missing from target_out_path, searches
    source_folder_path for a matching-architecture vendor .so and auto-injects it.
    Also verifies that undefined global dynamic symbols of elf_path are satisfied.
    Returns (is_valid: bool, missing_libs: list[str], missing_symbols: list[str]).
    """
    root_info = _extract_elf_info(elf_path)
    if root_info is None:
        return False, [f"Invalid ELF: {elf_path}"], []

    bits, root_needed, root_undef_syms, _ = root_info
    lib_dir_name = "lib64" if bits == 64 else "lib"

    search_dirs = [
        os.path.join(target_out_path, "system", lib_dir_name),
        os.path.join(target_out_path, "system", "fmd"),
        os.path.join(target_out_path, "system_ext", lib_dir_name),
        os.path.join(target_out_path, "product", lib_dir_name),
        os.path.join(target_out_path, "vendor", lib_dir_name),
    ]
    for apex_base in (os.path.join(target_out_path, "apex"), os.path.join(target_out_path, "system", "apex")):
        if os.path.isdir(apex_base):
            for entry in os.listdir(apex_base):
                cand_lib = os.path.join(apex_base, entry, lib_dir_name)
                if os.path.isdir(cand_lib):
                    search_dirs.append(cand_lib)

    # Index vendor source .so files lazily if needed
    vendor_so_index = None

    def _find_in_target(soname):
        for sdir in search_dirs:
            cand = os.path.join(sdir, soname)
            if os.path.isfile(cand):
                return cand
        return None

    def _find_and_pull_from_vendor(soname):
        nonlocal vendor_so_index
        if not source_folder_path or not os.path.isdir(source_folder_path):
            return None
        if vendor_so_index is None:
            vendor_so_index = defaultdict(list)
            for root, _, files in scandir_walk(source_folder_path):
                for fn in files:
                    if fn.endswith(".so"):
                        vendor_so_index[fn].append(os.path.join(root, fn))

        for cand_src in vendor_so_index.get(soname, []):
            cand_info = _extract_elf_info(cand_src)
            if cand_info and cand_info[0] == bits:
                # Determine appropriate destination partition directory
                dst_part = "vendor" if "/vendor/" in cand_src else (
                    "system_ext" if "/system_ext/" in cand_src else (
                        "product" if "/product/" in cand_src else "system"
                    )
                )
                dst_dir = os.path.join(target_out_path, dst_part, lib_dir_name)
                os.makedirs(dst_dir, exist_ok=True)
                dst_path = os.path.join(dst_dir, soname)
                shutil.copy2(cand_src, dst_path)
                set_executable_permission(dst_path)
                logging.info(f"Auto-pulled missing DT_NEEDED dependency {soname} from {cand_src} -> {dst_path}")
                return dst_path
        return None

    # Core APEX libraries that may reside inside compressed/mounted APEX containers at runtime
    apex_provided_sonames = {
        "libc.so", "libdl.so", "libdl_android.so", "libm.so",
        "libart.so", "libartbase.so", "libdexfile.so", "libnativehelper.so",
        "libnativebridge.so", "libnativeloader.so", "libsigchain.so",
        "libicuuc.so", "libicui18n.so", "libandroidicu.so", "libpac.so",
        "libstatssocket.so", "libstatspull.so",
    }

    visited = {}
    queue = list(root_needed)
    missing_libs = []
    exported_symbols = set()
    unmounted_apex_lib_encountered = False

    while queue:
        soname = queue.pop(0)
        if soname in visited:
            continue
        target_lib_path = _find_in_target(soname)
        if not target_lib_path:
            target_lib_path = _find_and_pull_from_vendor(soname)

        if not target_lib_path:
            if soname in apex_provided_sonames:
                unmounted_apex_lib_encountered = True
                visited[soname] = True
                continue
            missing_libs.append(soname)
            visited[soname] = False
            continue

        visited[soname] = True
        dep_info = _extract_elf_info(target_lib_path)
        if dep_info is None:
            missing_libs.append(soname)
            continue
        _, dep_needed, _, dep_defined = dep_info
        exported_symbols.update(dep_defined)
        for nxt in dep_needed:
            if nxt not in visited:
                queue.append(nxt)

    missing_symbols = []
    if not missing_libs and not unmounted_apex_lib_encountered and exported_symbols:
        for sym in sorted(root_undef_syms):
            if sym not in exported_symbols:
                missing_symbols.append(sym)

    is_valid = len(missing_libs) == 0 and len(missing_symbols) == 0
    return is_valid, missing_libs, missing_symbols


def _restore_core_group_snapshot(group_name, group_snap):
    for rel_path, entry in group_snap.items():
        if os.path.exists(entry["backup"]):
            os.makedirs(os.path.dirname(entry["target"]), exist_ok=True)
            shutil.copy2(entry["backup"], entry["target"])
            set_executable_permission(entry["target"])
        for obj_target, obj_backup in entry.get("obj_backups", []):
            if os.path.exists(obj_backup):
                shutil.copy2(obj_backup, obj_target)
                set_executable_permission(obj_target)
    logging.warning(f"Atomically rolled back coupled core group '{group_name}' to AOSP baseline.")


def validate_and_reconcile_coupled_core_groups(snapshots, source_folder_path, target_out_path, aosp_path=None):
    """
    Validates each coupled core group (zygote, system_server) after injection:
    1. Ensures Java (.jar) and JNI (.so) pairs are coupled (not half-vendor/half-AOSP).
    2. Ensures all .jar members contain valid classes.dex.
    3. Ensures all ELF (.so / binary) members pass transitive DT_NEEDED & symbol checks.
    If any check fails for a group, atomically rolls back the entire group to AOSP.
    Returns dict of { group_name: {"status": "kept_vendor"|"rolled_back"|"unchanged", "reason": str} }.
    """
    report = {}
    required_pairs = {
        "zygote": ("system/framework/framework.jar", "system/lib64/libandroid_runtime.so"),
        "system_server": ("system/framework/services.jar", "system/lib64/libandroid_servers.so"),
    }

    for group_name, group_snap in (snapshots or {}).items():
        if not group_snap:
            report[group_name] = {"status": "unchanged", "reason": "No baseline files tracked"}
            continue

        modified_members = []
        for rel_path, entry in group_snap.items():
            curr_md5 = get_md5_from_file(entry["target"]) if os.path.exists(entry["target"]) else None
            if curr_md5 != entry["md5"]:
                modified_members.append(rel_path)

        if not modified_members:
            report[group_name] = {"status": "unchanged", "reason": "No members modified"}
            continue

        failure_reasons = []
        # 1. Check primary Java + JNI pair coupling
        pair = required_pairs.get(group_name)
        if pair and pair[0] in group_snap and pair[1] in group_snap:
            jar_mod = pair[0] in modified_members
            so_mod = pair[1] in modified_members
            if jar_mod != so_mod:
                failure_reasons.append(
                    f"Uncoupled Java/JNI modification: {pair[0]} (modified={jar_mod}) vs {pair[1]} (modified={so_mod})"
                )

        # 2. Validate every existing/modified member in the group
        for rel_path, entry in group_snap.items():
            target_file = entry["target"]
            if not os.path.exists(target_file):
                failure_reasons.append(f"Missing core file: {rel_path}")
                continue
            if rel_path.endswith(".jar"):
                has_dex, dex_err = ensure_archive_has_dex(
                    target_file, aosp_path=aosp_path, source_root_path=source_folder_path, require_dex=True
                )
                if not has_dex:
                    failure_reasons.append(f"Invalid JAR {rel_path}: {dex_err}")
            elif is_elf_binary(target_file):
                elf_ok, missing_libs, missing_syms = resolve_and_validate_elf_dependencies(
                    target_file, source_folder_path, target_out_path
                )
                if not elf_ok:
                    failure_reasons.append(
                        f"ELF dependency/symbol failure in {rel_path}: missing_libs={missing_libs}, "
                        f"missing_symbols={missing_syms[:10]}"
                    )

        if failure_reasons:
            reason_str = "; ".join(failure_reasons)
            logging.warning(f"Coupled group '{group_name}' failed validation ({reason_str}). Rolling back to AOSP.")
            _restore_core_group_snapshot(group_name, group_snap)
            report[group_name] = {"status": "rolled_back", "reason": reason_str}
        else:
            logging.info(f"Coupled group '{group_name}' passed validation (modified={modified_members}).")
            report[group_name] = {"status": "kept_vendor", "reason": f"Validated {modified_members}"}

    return report


def clean_stale_precompiled_art_artifacts(target_out_path):
    """
    Removes stale precompiled AOSP boot images (boot*.art, boot*.oat, boot*.vdex) and
    framework oat/ directories so ART compiles fresh boot images matching injected .jar files.
    Returns list of removed paths.
    """
    removed = []
    if not target_out_path or not os.path.isdir(target_out_path):
        return removed

    for part in ("system", "system_ext", "product", "vendor"):
        fw_dir = os.path.join(target_out_path, part, "framework")
        if not os.path.isdir(fw_dir):
            continue
        # Remove <fw_dir>/oat/
        oat_dir = os.path.join(fw_dir, "oat")
        if os.path.isdir(oat_dir):
            try:
                shutil.rmtree(oat_dir)
                removed.append(oat_dir)
            except Exception as e:
                logging.warning(f"Failed to remove stale oat dir {oat_dir}: {e}")

        # Remove <fw_dir>/<arch>/boot* and <fw_dir>/boot*
        for pattern in (
            os.path.join(fw_dir, "*", "boot*"),
            os.path.join(fw_dir, "boot*.art"),
            os.path.join(fw_dir, "boot*.oat"),
            os.path.join(fw_dir, "boot*.vdex"),
        ):
            for match in glob.glob(pattern):
                if os.path.isfile(match) or os.path.islink(match):
                    try:
                        os.remove(match)
                        removed.append(match)
                    except Exception:
                        pass

    if removed:
        logging.info(f"Cleaned {len(removed)} stale precompiled ART boot/oat artifact(s) under {target_out_path}")
    return removed


def reconcile_init_environ_classpaths(source_folder_path, target_out_path, aosp_path=None):
    """
    Parses vendor init.environ.rc for BOOTCLASSPATH, DEX2OATBOOTCLASSPATH, and
    SYSTEMSERVERCLASSPATH entries. For any vendor .jar that exists in target_out_path
    and contains valid classes.dex, appends it to AOSP's init.environ.rc without
    removing AOSP's /apex/ classpath entries. Also removes any unrecoverable stripped
    .jar stubs from target_out_path/framework directories.
    Returns list of updated init.environ.rc paths.
    """
    updated_rc_files = []
    if not target_out_path or not os.path.isdir(target_out_path):
        return updated_rc_files

    # 1. Sweep framework directories in target_out_path for any stripped .jar stubs missing classes.dex
    for part in ("system", "system_ext", "product", "vendor"):
        fw_dir = os.path.join(target_out_path, part, "framework")
        if not os.path.isdir(fw_dir):
            continue
        for jar_file in glob.glob(os.path.join(fw_dir, "*.jar")):
            has_dex, _ = ensure_archive_has_dex(
                jar_file, aosp_path=aosp_path, source_root_path=source_folder_path, require_dex=True
            )
            if not has_dex:
                logging.warning(f"Removing unrecoverable stripped JAR stub from target image: {jar_file}")
                try:
                    os.remove(jar_file)
                except Exception:
                    pass

    if not source_folder_path or not os.path.isdir(source_folder_path):
        return updated_rc_files

    vendor_rc_candidates = [
        os.path.join(source_folder_path, "system", "etc", "init", "hw", "init.environ.rc"),
        os.path.join(source_folder_path, "system", "init.environ.rc"),
        os.path.join(source_folder_path, "system", "system", "etc", "init", "hw", "init.environ.rc"),
        os.path.join(source_folder_path, "init.environ.rc"),
    ]
    vendor_rc = next((p for p in vendor_rc_candidates if os.path.isfile(p)), None)
    if not vendor_rc:
        return updated_rc_files

    target_rc_candidates = [
        os.path.join(target_out_path, "system", "etc", "init", "hw", "init.environ.rc"),
        os.path.join(target_out_path, "root", "init.environ.rc"),
        os.path.join(target_out_path, "system", "init.environ.rc"),
    ]
    target_rcs = [p for p in target_rc_candidates if os.path.isfile(p)]
    if not target_rcs:
        return updated_rc_files

    classpath_vars = ("BOOTCLASSPATH", "DEX2OATBOOTCLASSPATH", "SYSTEMSERVERCLASSPATH")
    vendor_cp_entries = defaultdict(list)

    try:
        with open(vendor_rc, "r", encoding="utf-8", errors="ignore") as vf:
            for line in vf:
                m = re.match(r"^\s*export\s+(BOOTCLASSPATH|DEX2OATBOOTCLASSPATH|SYSTEMSERVERCLASSPATH)\s+(.+)$", line.strip())
                if m:
                    var_name, raw_val = m.group(1), m.group(2).strip()
                    for entry in raw_val.split(":"):
                        entry = entry.strip()
                        if entry and not entry.startswith("/apex/") and entry.endswith(".jar"):
                            if entry not in vendor_cp_entries[var_name]:
                                vendor_cp_entries[var_name].append(entry)
    except Exception as e:
        logging.warning(f"Failed to parse vendor init.environ.rc ({vendor_rc}): {e}")
        return updated_rc_files

    # Filter vendor_cp_entries to only JARs that actually exist and contain classes.dex in target_out_path
    verified_cp_entries = defaultdict(list)
    for var_name, entries in vendor_cp_entries.items():
        for dev_path in entries:
            rel_path = dev_path.lstrip("/")
            host_path = os.path.join(target_out_path, rel_path)
            if os.path.isfile(host_path):
                has_dex, _ = ensure_archive_has_dex(
                    host_path, aosp_path=aosp_path, source_root_path=source_folder_path, require_dex=True
                )
                if has_dex:
                    verified_cp_entries[var_name].append(dev_path)

    if not any(verified_cp_entries.values()):
        return updated_rc_files

    for trc in target_rcs:
        try:
            with open(trc, "r", encoding="utf-8", errors="ignore") as tf:
                lines = tf.readlines()

            new_lines = []
            modified = False
            for line in lines:
                m = re.match(r"^(\s*export\s+(BOOTCLASSPATH|DEX2OATBOOTCLASSPATH|SYSTEMSERVERCLASSPATH)\s+)(.+)$", line)
                if m:
                    prefix, var_name, curr_val = m.group(1), m.group(2), m.group(3).strip()
                    curr_parts = [p.strip() for p in curr_val.split(":") if p.strip()]
                    for cand in verified_cp_entries.get(var_name, []):
                        if cand not in curr_parts:
                            curr_parts.append(cand)
                            modified = True
                    new_lines.append(f"{prefix}{':'.join(curr_parts)}\n")
                else:
                    new_lines.append(line)

            if modified:
                with open(trc, "w", encoding="utf-8") as tf:
                    tf.writelines(new_lines)
                updated_rc_files.append(trc)
                logging.info(f"Updated {trc} with verified vendor classpath JARs: {dict(verified_cp_entries)}")
        except Exception as e:
            logging.warning(f"Failed to update target init.environ.rc ({trc}): {e}")

    return updated_rc_files


def sync_vendor_feature_build_props(source_folder_path, target_out_path):
    """
    Extracts non-hardware OEM feature-flag and branding properties (e.g. ro.miui.*,
    ro.oplus.*, ro.build.version.oplusrom, ro.build.version.oneui, ro.config.*)
    from vendor build.prop files and appends any missing keys to AOSP's system/build.prop
    while enforcing ro.control_privapp_permissions=log.
    Returns count of synchronized properties.
    """
    if not source_folder_path or not target_out_path:
        return 0
    if not os.path.isdir(source_folder_path) or not os.path.isdir(target_out_path):
        return 0

    vendor_props = {}
    for root, _, files in scandir_walk(source_folder_path):
        for fn in files:
            if fn.endswith("build.prop"):
                prop_path = os.path.join(root, fn)
                try:
                    with open(prop_path, "r", encoding="utf-8", errors="ignore") as pf:
                        for line in pf:
                            stripped = line.strip()
                            if not stripped or stripped.startswith("#") or "=" not in stripped:
                                continue
                            k, v = stripped.split("=", 1)
                            k = k.strip()
                            v = v.strip()
                            if any(k.startswith(up) for up in UNSAFE_VENDOR_PROP_PREFIXES):
                                continue
                            if any(k.startswith(sp) for sp in SAFE_VENDOR_PROP_PREFIXES):
                                vendor_props.setdefault(k, v)
                except Exception:
                    pass

    target_prop_candidates = [
        os.path.join(target_out_path, "system", "build.prop"),
        os.path.join(target_out_path, "system", "system", "build.prop"),
    ]
    target_prop_path = next((p for p in target_prop_candidates if os.path.isfile(p)), None)
    if not target_prop_path:
        return 0

    existing_keys = set()
    lines = []
    try:
        with open(target_prop_path, "r", encoding="utf-8", errors="ignore") as tf:
            for line in tf:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k = stripped.split("=", 1)[0].strip()
                    existing_keys.add(k)
                    if k == "ro.control_privapp_permissions":
                        lines.append("ro.control_privapp_permissions=log\n")
                        continue
                lines.append(line if line.endswith("\n") else f"{line}\n")
    except Exception as e:
        logging.warning(f"Failed to read {target_prop_path}: {e}")
        return 0

    added = 0
    if "ro.control_privapp_permissions" not in existing_keys:
        lines.append("ro.control_privapp_permissions=log\n")
        added += 1

    for k, v in sorted(vendor_props.items()):
        if k not in existing_keys:
            lines.append(f"{k}={v}\n")
            existing_keys.add(k)
            added += 1

    if added > 0:
        try:
            with open(target_prop_path, "w", encoding="utf-8") as tf:
                tf.writelines(lines)
            logging.info(f"Synchronized {added} OEM feature/safety build.prop entries into {target_prop_path}")
        except Exception as e:
            logging.warning(f"Failed to write synchronized properties to {target_prop_path}: {e}")
            return 0

    return added




def start_post_build_injector(aosp_path,
                              source_folder_path,
                              target_out_path,
                              lunch_target,
                              firmware_id=None, # REMOVE
                              pre_injector_package_list=None,
                              pre_injector_config_path=None,
                              post_injector_config_path=None,
                              cookies=None, # REMOVE
                              aosp_version=None,
                              partition_list=[""],
                              tag=None):
    """
    Start the post build injector. Replaces the original objects in the AOSP source code with the vendor flavoured
    objects.

    :param aosp_path: str - path to the AOSP source code.
    :param source_folder_path: str - path to the source folder where the objects to inject reside.
    :param target_out_path: str - path to the AOSP target out folder.
    :param lunch_target: str - lunch target for the AOSP build.
    """
    if pre_injector_package_list is None:
        pre_injector_package_list = []
    logging.info(
        f"Starting post build injector with config:: {post_injector_config_path} | {pre_injector_config_path}")
    pre_injector_config, post_injector_config = load_configs(pre_injector_config_path, post_injector_config_path)
    global PRE_INJECTOR_CONFIG
    global POST_INJECTOR_CONFIG
    PRE_INJECTOR_CONFIG = pre_injector_config
    POST_INJECTOR_CONFIG = post_injector_config
    logging.debug(f"Starting post build injector with the following configuration: {POST_INJECTOR_CONFIG} | {PRE_INJECTOR_CONFIG} | {post_injector_config_path} | {pre_injector_config_path}")
    if not aosp_path.endswith("/"):
        aosp_path = f"{aosp_path}/"
        if not os.path.exists(aosp_path):
            logging.error(f"AOSP source folder does not exist: {aosp_path}")
            raise FileNotFoundError(f"AOSP path does not exist: {aosp_path}")

    logging.info(
        f"Starting post build injector: {aosp_path} | {source_folder_path} | {target_out_path} | {lunch_target}")

    # Improved checks with separate logs to help debugging the root cause when post-injection fails
    if not os.path.exists(source_folder_path):
        logging.error(f"Source folder does not exist: {source_folder_path}")
        raise FileNotFoundError(f"Post-Injection source folder does not exist: {source_folder_path}")

    if not os.path.isdir(source_folder_path):
        logging.error(f"Source path exists but is not a directory: {source_folder_path}")
        raise NotADirectoryError(f"Post-Injection source path is not a directory: {source_folder_path}")

    try:
        entries = os.listdir(source_folder_path)
    except Exception as e:
        logging.error(f"Could not list contents of source folder {source_folder_path}: {e}")
        raise

    if not entries:
        logging.error(f"Source folder is empty: {source_folder_path}")
        raise FileNotFoundError(f"Post-Injection source folder is empty: {source_folder_path}")

    if POST_INJECTOR_CONFIG["ENABLE_INJECTION"]:
        with Executor() as executor:
            inject(aosp_path,
                   source_folder_path,
                   target_out_path,
                   executor,
                   lunch_target,
                   firmware_id,
                   pre_injector_package_list,
                   cookies,
                   aosp_version,
                   partition_list,
                   tag)
    else:
        logging.info(f"Post-Injection is disabled by configuration: {POST_INJECTOR_CONFIG['ENABLE_INJECTION']}")
        logging.info(f"Skipping post build injection for {source_folder_path} into {target_out_path}")

    logging.debug(f"Finished post build injector")




def group_errors_by_prefix(error_list):
    """
    Groups errors by the first three words and counts occurrences.

    :param error_list: list - List of error messages.
    :return: dict - Grouped errors with counts.
    """
    error_groups = defaultdict(int)
    error_sample_list = {}
    for error in error_list:
        # Extract the first three words
        match = re.match(r"(\S+\s+\S+\s+\S+)", error)
        if match:
            prefix = match.group(1)
            error_groups[prefix] += 1
            if prefix not in error_sample_list:
                error_sample_list[prefix] = error
        else:
            # If no match, group under "Unknown Errors"
            error_groups["Unknown Errors"] += 1
    return error_groups, error_sample_list

def extract_file_type_frequencies(error_list):
    """
    Extracts and counts file types from error messages that end with a file path.

    :param error_list: list - List of error messages.
    :return: dict - Frequency of file types.
    """
    file_type_counts = defaultdict(int)
    for error in error_list:
        # Match file paths at the end of the error message and extract the file extension
        match = re.search(r".*\.(\w+)$", error)
        if match:
            file_extension = match.group(1).lower()  # Extract and normalize the file extension
            file_type_counts[file_extension] += 1
    return file_type_counts

def count_number_of_extracted_files(source_folder_path):
    """
    Counts the number of files in the source folder across all standard and OEM partitions.

    :param source_folder_path: str - Path to the source folder.
    :return: dict - Number of files in the source folder per partition.
    """
    partition_names = PARTITION_NAME_LIST
    file_count_per_partition = defaultdict(int)
    for partition_name in partition_names:
        partition_path = os.path.join(source_folder_path, partition_name)
        if os.path.exists(partition_path):
            for root, _, files in os.walk(partition_path):
                file_count_per_partition[partition_name] += len(files)
    return file_count_per_partition



def build_intermediate_file_index(aosp_path, target_out_path):
    global INTERMEDIATE_MD5_MAP
    # Path to cached md5 map

    try:
        # Try to load precomputed INTERMEDIATE_MD5_MAP from disk to avoid expensive rebuild
        if os.path.exists(FILE_MAP_PATH):
            try:
                with open(FILE_MAP_PATH, "r") as f:
                    loaded = json.load(f)
                INTERMEDIATE_MD5_MAP = MappingProxyType({k: tuple(v) for k, v in loaded.items()})
                logging.info(f"Loaded INTERMEDIATE_MD5_MAP from {FILE_MAP_PATH} with {len(INTERMEDIATE_MD5_MAP)} entries")
            except Exception as e:
                logging.warning(f"Failed to load INTERMEDIATE_MD5_MAP from {FILE_MAP_PATH}: {e}")
                # fall through to rebuild if loading fails
        if INTERMEDIATE_MD5_MAP is None:
            # Removed redundant str() wrappers
            intermediate_path_list = [
                os.path.join(aosp_path, "out/soong/.intermediates/"),
                os.path.join(aosp_path, "out/target/product/emulator64_arm64/apex/"),
                os.path.join(aosp_path, "out/target/product/emu64a/apex/"),
                os.path.join(aosp_path, "out/target/product/emu64a/obj/"),
                os.path.join(aosp_path, "out/target/product/emulator64_arm64/obj/"),
            ]

            exclude_list = [
                os.path.join(aosp_path, "out/soong/.intermediates/prebuilts")
            ]

            combined_md5_map = defaultdict(list)

            num_tasks = len(intermediate_path_list)
            cpu_count = os.cpu_count() or 1
            optimal_workers = min(num_tasks, cpu_count)

            with concurrent.futures.ProcessPoolExecutor(max_workers=optimal_workers) as executor:
                # Map futures to their paths for error logging
                futures = {
                    executor.submit(build_intermediate_md5_map, aosp_path, path, exclude_dirs=exclude_list): path
                    for path in intermediate_path_list
                }

                for future in concurrent.futures.as_completed(futures):
                    path = futures[future]
                    try:
                        partial_map = future.result()
                        for md5_key, paths_list in partial_map.items():
                            combined_md5_map[md5_key].extend(paths_list)
                    except Exception as exc:
                        logging.warning(f"Error processing intermediate path {path}: {exc}")

            INTERMEDIATE_MD5_MAP = MappingProxyType({
                k: tuple(v) for k, v in combined_md5_map.items()
            })
            logging.info(f"Initialized INTERMEDIATE_MD5_MAP with {len(INTERMEDIATE_MD5_MAP)} entries")

            # attempt to persist map to disk
            try:
                os.makedirs(os.path.dirname(FILE_MAP_PATH), exist_ok=True)
                tmp_path = f"{FILE_MAP_PATH}.tmp"
                with open(tmp_path, "w") as f:
                    json.dump({k: list(v) for k, v in combined_md5_map.items()}, f)
                os.replace(tmp_path, FILE_MAP_PATH)
                logging.info(f"Saved INTERMEDIATE_MD5_MAP to {FILE_MAP_PATH}")
            except Exception as e:
                logging.warning(f"Failed to save INTERMEDIATE_MD5_MAP to {FILE_MAP_PATH}: {e}")
    except Exception as e:
        logging.warning(f"Failed to build or load INTERMEDIATE_MD5_MAP: {e}")

    # 2. DOUBLE-CHECKED LOCKING
    target_obj_path = None
    try:
        target_obj_path = os.path.join(target_out_path, FOLDER_NAME_OBJECTS)

        # Unlocked read check prevents blocking after initialization
        if target_obj_path not in FILE_INDEX_CACHE:
            with FILE_INDEX_LOCK:
                # Locked read check prevents race condition if two threads miss the first check
                if target_obj_path not in FILE_INDEX_CACHE:
                    idx = defaultdict(list)
                    if os.path.exists(target_obj_path):
                        for root, dirs, files in scandir_walk(target_obj_path):
                            for f in files:
                                idx[f].append(os.path.join(root, f))

                    FILE_INDEX_CACHE[target_obj_path] = idx
                    logging.info(
                        f"Built file index for {target_obj_path} with {sum(len(v) for v in idx.values())} entries")
    except Exception as e:
        logging.warning(f"Failed to build file index for target objects: {e}")

    return target_obj_path


def create_error_statistics(error_list):
    """
    Analyze skipped errors and produce statistics about skipped file types.

    The function extracts file paths from error messages that indicate a skipped file
    (messages like: "Skipped File post-inject ...: /path/to/file | module_type: SKIPPED")
    and computes frequency counts of file extensions. Files without an extension are
    recorded under the key "<no_extension>" and — when the file exists on disk —
    additionally checked for ELF magic to estimate how many no-extension files are
    ELF binaries.

    The resulting statistics are logged and written to `SKIPPED_ERROR_LIST_FILE_PATH`.

    :param error_list: list of error message strings
    :return: dict with statistics
    """
    skipped_paths = []
    # Pattern to capture a filesystem path after the colon and before the pipe
    path_re = re.compile(r'Skipped File[^"]*:\s*(/[^|\n]+)\s*\|', re.IGNORECASE)

    for err in error_list:
        m = path_re.search(err)
        if m:
            path = m.group(1).strip()
            skipped_paths.append(path)

    total_skipped = len(skipped_paths)

    ext_counts = defaultdict(int)
    ext_samples = defaultdict(list)
    no_ext_elf_count = 0
    no_ext_paths = []

    for p in skipped_paths:
        _, ext = os.path.splitext(p)
        ext = ext.lower()
        if not ext:
            key = "<no_extension>"
            no_ext_paths.append(p)
            # If file exists try to detect ELF magic
            try:
                if os.path.exists(p) and os.path.isfile(p):
                    with open(p, 'rb') as fh:
                        hdr = fh.read(4)
                        if hdr == b'\x7fELF':
                            no_ext_elf_count += 1
            except Exception:
                # ignore any IO errors while probing files
                pass
        else:
            key = ext

        ext_counts[key] += 1
        if len(ext_samples[key]) < 5:
            ext_samples[key].append(p)

    # Prepare sorted list of extensions by frequency
    sorted_ext = sorted(ext_counts.items(), key=lambda kv: kv[1], reverse=True)

    stats = {
        "total_skipped": total_skipped,
        "extension_counts": dict(sorted_ext),
        "extension_samples": {k: v for k, v in ext_samples.items()},
        "no_extension_count": len(no_ext_paths),
        "no_extension_elf_count": no_ext_elf_count,
        "no_extension_examples": no_ext_paths[:10]
    }

    logging.info("Skipped files summary: total_skipped=%d", total_skipped)
    for ext, cnt in sorted_ext:
        logging.info("Skipped extension %s: %d occurrences", ext, cnt)

    if stats["no_extension_count"]:
        logging.info("Files without extension: %d (ELF detected: %d)", stats["no_extension_count"], stats["no_extension_elf_count"]) 

    # Persist statistics to the skipped error list path for further analysis
    try:
        write_json_output(stats, SKIPPED_ERROR_LIST_FILE_PATH)
    except Exception as e:
        logging.warning(f"Could not write skipped error statistics to {SKIPPED_ERROR_LIST_FILE_PATH}: {e}")

    # Additionally compute path-frequency statistics for files under the extracted ALL_FILES root
    try:
        all_files_prefix = None
        try:
            all_files_prefix = os.path.abspath(EXTRACTION_ALL_FILES_PATH)
        except Exception:
            logging.error(f"Could not resolve absolute path for ALL_FILES: {EXTRACTION_ALL_FILES_PATH}")
            return

        path_counts = defaultdict(int)
        path_samples = defaultdict(list)

        for p in skipped_paths:
            try:
                abs_p = os.path.abspath(p)
            except Exception:
                continue
            if not abs_p.startswith(all_files_prefix + os.sep) and abs_p != all_files_prefix:
                # skip files not under the ALL_FILES prefix
                continue

            # compute relative components under ALL_FILES
            rel = abs_p[len(all_files_prefix):].lstrip(os.sep)
            parts = rel.split(os.sep)
            # drop trailing filename if present
            if len(parts) == 0:
                continue
            # if last component contains a dot and looks like a filename, exclude it from directory components
            dir_parts = parts[:-1] if len(parts) > 1 else []

            # create subpath fragments of depth 1..4 starting at any position to capture patterns like /etc/security
            max_depth = 4
            for i in range(len(dir_parts)):
                for depth in range(1, min(max_depth, len(dir_parts) - i) + 1):
                    sub = "/" + "/".join(dir_parts[i:i+depth])
                    path_counts[sub] += 1
                    if len(path_samples[sub]) < 5:
                        path_samples[sub].append(abs_p)

        # Prepare sorted list of path frequencies (most common first)
        sorted_paths = sorted(path_counts.items(), key=lambda kv: kv[1], reverse=True)
        stats["all_files_prefix"] = all_files_prefix
        stats["all_files_path_counts"] = dict(sorted_paths[:200])
        stats["all_files_path_samples"] = {k: v for k, v in path_samples.items() if k in dict(sorted_paths[:200])}

        logging.info("Top skipped paths under ALL_FILES:")
        for path_key, count in sorted_paths[:20]:
            logging.info("%s: %d", path_key, count)

        # persist extended stats as well
        try:
            write_json_output(stats, SKIPPED_ERROR_LIST_FILE_PATH)
        except Exception:
            pass
    except Exception as e:
        logging.debug(f"Error while computing ALL_FILES path frequency stats: {e}")

    return stats

def inject(aosp_path,
           source_folder_path,
           target_out_path,
           executor,
           lunch_target,
           firmware_id,
           pre_injector_package_list,
           cookies,
           aosp_version,
           partition_list,
           tag):

    folder_path_list = get_folders(source_folder_path, partition_list)
    logging.info(f"Folder path list: {folder_path_list}")
    pre_execution_time_minutes = -1
    if len(folder_path_list) > 0:
        start_preprocessing = time.time()
        logging.info(f"Start preprocessing {len(folder_path_list)} folders: {start_preprocessing}")
        target_obj_path = build_intermediate_file_index(aosp_path, target_out_path)
        end_preprocessing = time.time()
        pre_execution_time_minutes = (end_preprocessing - start_preprocessing) / 60
        logging.info(f"Preprocessing completed in {pre_execution_time_minutes:.2f} minutes")


    start_time = time.time()
    logging.info(f"Injection started at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_time))}")
    global AOSP_APK_INDEX
    core_group_snapshots = {}
    try:
        core_group_snapshots = snapshot_aosp_core_groups(target_out_path)
    except Exception as e:
        logging.warning(f"Failed to snapshot coupled core groups: {e}")

    try:
        AOSP_APK_INDEX = build_aosp_apk_index(target_out_path, aosp_path=aosp_path)
    except Exception as e:
        logging.warning(f"Failed to build initial AOSP APK index: {e}")
        AOSP_APK_INDEX = None

    error_list, inj_obj_list, inj_partition_list, acv_result_dict = process_partitions(aosp_path,
                       target_out_path,
                       executor,
                       lunch_target,
                       pre_injector_package_list,
                       firmware_id,
                       cookies,
                       aosp_version,
                       tag,
                       folder_path_list)
    # Post-injection Zygote / SystemServer / Pre-installed App hardening passes
    try:
        validate_and_reconcile_coupled_core_groups(
            core_group_snapshots, source_folder_path, target_out_path, aosp_path=aosp_path
        )
    except Exception as e:
        logging.error(f"Error validating coupled core groups: {e}")

    try:
        clean_stale_precompiled_art_artifacts(target_out_path)
    except Exception as e:
        logging.error(f"Error cleaning stale ART boot/oat artifacts: {e}")

    try:
        reconcile_init_environ_classpaths(source_folder_path, target_out_path, aosp_path=aosp_path)
    except Exception as e:
        logging.error(f"Error reconciling init.environ.rc classpaths: {e}")

    try:
        sync_vendor_feature_build_props(source_folder_path, target_out_path)
    except Exception as e:
        logging.error(f"Error synchronizing vendor feature build.prop entries: {e}")

    try:
        generate_privapp_permissions_allowlist(target_out_path, aosp_path=aosp_path)
    except Exception as e:
        logging.error(f"Error generating privapp-permissions allowlists: {e}")

    # Verify Direct Injection targets exist on filesystem and log errors for missing targets
    try:
        direct_inject_errors = verify_direct_injection_targets(inj_partition_list)
        if direct_inject_errors:
            error_list.extend(direct_inject_errors)
    except Exception as e:
        logging.error(f"Error while verifying Direct Injection targets: {e}")
    end_time = time.time()

    # free caches
    INTERMEDIATE_MD5_MAP = None

    try:
        if 'target_obj_path' in locals():
            with FILE_INDEX_LOCK:
                if target_obj_path in FILE_INDEX_CACHE:
                    del FILE_INDEX_CACHE[target_obj_path]
    except Exception:
        pass

    logging.info(f"Injection ended at {end_time}")
    execution_time = end_time - start_time
    execution_time_minutes = execution_time / 60
    logging.info(f"Objects injected:")
    app_list = []
    apex_list = []
    libs_list = []
    skipped_app_list = []
    skipped_apex_list = []
    skipped_libs_list = []
    if PRINT_ALL_LOGS:
        for obj in inj_obj_list:
            logging.info(f"Indirect Inject via obj: {obj}")
            if isinstance(obj, tuple) and obj and isinstance(obj[0], str) and obj[0].endswith(".apk"):
                file_name = os.path.basename(obj[0])
                app_list.append(file_name)
            elif isinstance(obj, tuple) and obj and isinstance(obj[0], str) and obj[0].endswith(".apex"):
                file_name = os.path.basename(obj[0])
                apex_list.append(file_name)
            elif isinstance(obj, tuple) and obj and isinstance(obj[0], str) and obj[0].endswith(".so"):
                file_name = os.path.basename(obj[0])
                libs_list.append(file_name)

        logging.info(f"Partition files injected:")
        for obj in inj_partition_list:
            logging.info(f"Direct Inject: {obj}")
            if isinstance(obj, tuple) and obj and isinstance(obj[0], str) and obj[0].endswith(".apk"):
                file_name = os.path.basename(obj[0])
                app_list.append(file_name)
            elif isinstance(obj, tuple) and obj and isinstance(obj[0], str) and obj[0].endswith(".apex"):
                file_name = os.path.basename(obj[0])
                apex_list.append(file_name)
            elif isinstance(obj, tuple) and obj and isinstance(obj[0], str) and obj[0].endswith(".so"):
                file_name = os.path.basename(obj[0])
                libs_list.append(file_name)
    if PRINT_ERROR_LOGS:
        logging.info(f"Errors:")
        for obj in error_list:
            match = re.search(r":\s*(/[^|]+)\s*\|", obj)
            if match:
                file_path = match.group(1).strip()
                file_name = os.path.basename(file_path)
                if ".apk" in obj:
                    if not ".apk.idsig" not in obj:
                        skipped_app_list.append(file_name)
                if ".apex" in obj:
                    if "original_apex" not in obj:
                        skipped_apex_list.append(file_name)
                if ".so" in obj:
                    skipped_libs_list.append(file_name)
    logging.info(f"============================================================")
    logging.info(f"============================== ERRORS ==============================")
    logging.info(f"============================================================")
    for error in error_list:
        logging.info(f"{error}")
        if "_apex" in error or "_capex" in error:
            error_list.remove(error)
    logging.info(f"============================================================")
    logging.info(f"============================== Injected by Module Type ==============================")
    logging.info(f"============================================================")
    logging.info(f"\n\nInjected Apps/APEX/Libraries Summary:")
    logging.info(f"Post-Injection Apps injected: {app_list}\n\n")
    logging.info(f"Post-Injection APEX injected: {apex_list}\n\n")
    logging.info(f"Post-Injection Libraries injected: {libs_list}\n\n")
    logging.info(f"\nSkipped Apps/APEX/Libraries Summary:")
    logging.info(f"Post-Injection Libraries skipped: {skipped_libs_list}\n\n")
    logging.info(f"Post-Injection Apps skipped: {skipped_app_list}\n\n")
    logging.info(f"Post-Injection APEX skipped: {skipped_apex_list}\n\n")
    logging.info(f"============================================================")
    logging.info(f"============================== GROUPED ERRORS ==============================")
    logging.info(f"============================================================")
    grouped_errors, error_sample_list = group_errors_by_prefix(error_list)
    logging.info(f"Grouped Errors:")
    for prefix, count in grouped_errors.items():
        logging.info(f"{prefix} {count} occurrences")

    for prefix, sample in error_sample_list.items():
        logging.info(f"Sample Error for {prefix}: {sample}")

    file_type_frequencies = extract_file_type_frequencies(error_list)

    logging.info(f"File Type Frequencies:")
    for file_type, count in file_type_frequencies.items():
        logging.info(f".{file_type}: {count} occurrences")

    logging.info(f"============================================================")
    logging.info(f"============================== INJ-METRICS ==============================")
    logging.info(f"============================================================")
    logging.info(f"Pre-Processing time: {pre_execution_time_minutes} minutes")
    logging.info(f"acv_result_dict: {acv_result_dict}")
    logging.info(f"Execution time: {execution_time_minutes} minutes")
    number_of_files = count_number_of_extracted_files(source_folder_path)
    logging.info(f"Number of File in ALL_FILES: {number_of_files}")
    logging.info(f"Number of errors: {len(error_list)}")
    logging.info(f"Number of objects injected: {len(inj_obj_list)}")
    logging.info(f"Number of partition files injected: {len(inj_partition_list)}")
    logging.info(f"Number of files processed: {len(error_list) + len(inj_obj_list) + len(inj_partition_list)}")
    logging.info(f"Post-Injection Apps injected: {len(app_list)}")
    logging.info(f"Post-Injection APEX injected: {len(apex_list)}")
    logging.info(f"Post-Injection Libraries injected: {len(libs_list)}")
    logging.info(f"============================================================")
    result = {
        "hostname": os.uname()[1],
        "firmware_id": firmware_id,
        "method": "start_post_build_injector",
        "start_time": start_time,
        "end_time": end_time,
        "duration_minutes": execution_time_minutes,
        "duration_seconds": execution_time,
        "errors": len(error_list),
        "objects_injected": len(inj_obj_list),
        "partition_files_injected": len(inj_partition_list),
        "files_injected": len(inj_obj_list) + len(inj_partition_list),
        "errors_file_type_frequencies": file_type_frequencies,
        "errors_grouped": grouped_errors,
        "acv_results": acv_result_dict,
    }

    write_json_output(result, PATH_EXECUTION_TIME_LOG)
    stats = create_error_statistics(error_list)
    if stats:
        write_json_output(stats, SKIPPED_ERROR_LIST_FILE_PATH)


def get_folders(folder_path, folder_filters):
    """
    Get the partition folders to process, including OEM custom partitions when present.
    """
    logging.debug(f"Directory path: {folder_path}, folder filters: {folder_filters}")
    if not folder_filters or folder_filters == [""]:
        effective_filters = set(PARTITION_NAME_LIST)
    else:
        effective_filters = set(folder_filters) | set(OEM_CUSTOM_PARTITION_MAP.keys())

    folders = []
    for entry in os.listdir(folder_path):
        full_path = os.path.join(folder_path, entry)
        if os.path.isdir(full_path):
            folders.append(full_path)
    logging.debug(f"Folders: {folders}")

    result_list = []
    for entry in folders:
        dir_name = os.path.basename(entry)
        logging.debug(f"Directory name: {dir_name}")
        if dir_name in effective_filters:
            result_list.append(entry)

    logging.debug(f"Folders to process: {result_list}")
    return result_list



def process_partitions(aosp_path,
                       target_out_path,
                       executor,
                       lunch_target,
                       pre_injector_package_list,
                       firmware_id,
                       cookies,
                       aosp_version,
                       tag,
                       folder_path_list):

    combined_error_list = []
    combined_inj_obj_list = []
    combined_inj_partition_list = []
    acv_result_dict = {}

    for i, folder_path in enumerate(tqdm(folder_path_list, desc="Processing partitions")):
        if POST_INJECTOR_CONFIG["ENABLE_ACVTOOL_INSTRUMENTATION"]:
            start_time = time.time()
            acv_result_dict = add_acvtool_instrumentation_multiprocessing(
                firmware_id,
                aosp_path,
                cookies,
                folder_path,
                version=aosp_version,
                lunch_target=lunch_target,
                tag=tag,
                delete_instrumented_apks=ACVTOOL_DELETE_INSTRUMENTED_APKS,
                post_injector_config=POST_INJECTOR_CONFIG,
                upload_data=True
            )
            acv_end_time = time.time()
            execution_time = acv_end_time - start_time
            execution_time = execution_time / 60
            acv_result_dict["execution_time"] = execution_time

        error_list, inj_obj_list, inj_partition_list = process_partition_files(aosp_path,
                                                                               folder_path,
                                                                               target_out_path,
                                                                               executor,
                                                                               lunch_target,
                                                                               pre_injector_package_list,
                                                                               firmware_id,
                                                                               cookies,
                                                                               aosp_version)
        combined_error_list.extend(error_list)
        combined_inj_obj_list.extend(inj_obj_list)
        combined_inj_partition_list.extend(inj_partition_list)

    return combined_error_list, combined_inj_obj_list, combined_inj_partition_list, acv_result_dict


def process_file_concurrently(aosp_path, file_path, partition_name, target_out_path, lunch_target, pre_injector_package_list, firmware_id, cookies, aosp_version):
    inj_obj = None
    inj_partition = None
    error_message = None
    apex_merge_file_path_list = None
    lock_path = f"{file_path}.fmd-aecs-lock"
    processed_marker = f"{file_path}.fmd-aecs-processed"
    lock = FileLock(lock_path)

    if os.path.exists(processed_marker):
        return f"File already processed: {file_path}", None, None

    try:
        with ((((lock)))):
            if os.path.exists(processed_marker):
                return f"File already processed: {file_path}", None, None
            module_type, tmp_module_type = get_module_type(file_path,
                                          pre_injector_package_list=pre_injector_package_list,
                                          post_injector_config=POST_INJECTOR_CONFIG)

            #with processed_files_lock:
                #if file_path in processed_files:
                #    return f"File already processed: {file_path}", None, None
                #processed_files.add(file_path)

            if module_type in ["SKIPPED"]:
                if "_apex" in file_path:
                    error_message = f"File Ignored (Keyword/Extension/Filename): {file_path} | module_type: {module_type}"
                else:
                    error_message = f"Skipped File (Keyword/Extension/Filename): {file_path} | module_type: {module_type}"
                logging.info(error_message)
            else:
                logging.debug(f"Processing file {file_path}")
                filename = os.path.basename(file_path)
                file_extension = os.path.splitext(file_path)[1]
                if module_type == "APPS" and file_extension.lower() == ".apk":
                    target_apk_inj_path, _ = get_target_injection_path(file_path, partition_name, target_out_path)
                    with AOSP_APK_INDEX_LOCK:
                        allow_apk, dedup_reason = reconcile_and_deduplicate_vendor_apk(
                            file_path,
                            target_apk_inj_path,
                            target_out_path,
                            aosp_apk_index=AOSP_APK_INDEX,
                            aosp_path=aosp_path,
                        )
                    if not allow_apk:
                        error_message = f"Skipped File (Protected AOSP Package): {file_path} | {dedup_reason}"
                    else:
                        error_message = handle_app_modules(file_path, aosp_path, firmware_id, cookies)
                elif file_extension.lower() == ".jar":
                    error_message = handle_jar_module(
                        file_path,
                        aosp_path=aosp_path,
                        source_root_path=EXTRACTION_ALL_FILES_PATH,
                    )
                elif file_extension.lower() == ".apex" or file_extension.lower() == ".capex":
                    if file_path.endswith(".capex"):
                        file_path = replace_capex_with_apex(file_path)
                    if "tzdata" in file_path or "tzdata" in filename:
                        new_name = re.sub(r'tzdata\d+', 'tzdata', filename)
                        file_path = rename_file(file_path, new_name)
                    if aosp_version and float(aosp_version) > 12:
                        if "bluetooth" in filename:
                            new_name = filename.replace("bluetooth", "btservices")
                            file_path = rename_file(file_path, new_name)

                    allow_merge = True
                    if "vndk" in filename and not "current" in filename:
                        allow_merge = False

                    if POST_INJECTOR_CONFIG["ALLOW_APEX_INJECTION_MERGE"] \
                        and any(keyword in filename for keyword in POST_INJECTOR_CONFIG["ALLOW_APEX_MERGE_KEYWORD_LIST"]) \
                        and "ALL_FILES/system/" in file_path \
                        and allow_merge:
                        logging.debug(f"Handle APEX file: {file_path} with module type: {module_type}")
                        try:
                            is_merge_success, log_message, apex_merge_file_path_list = handle_apex_modules(file_path, aosp_path, lunch_target, target_out_path, aosp_version)
                        except Exception as e:
                            is_merge_success = False
                            log_message = f"Exception occurred: {e}:{traceback.format_exc()}\n{traceback.print_stack()}"

                        if not is_merge_success:
                            error_message = f"Error handling merge APEX file: {file_path}|{log_message}"
                        else:
                            error_message = None
                    else:
                        try:
                            is_repack_success, log_message = repackage_apex_file(aosp_path, file_path, lunch_target, aosp_version)
                            if not is_repack_success:
                                error_message = f"Error handling repack APEX file: {file_path}|{log_message}"
                                raise RuntimeError(error_message)
                            else:
                                error_message = None
                        except Exception as e:
                            error_message = f"Exception occurred: {e}:{traceback.format_exc()}\n{traceback.print_stack()}"
                elif module_type == "EXECUTABLES" and is_elf_binary(file_path):
                    if filename in POST_INJECTOR_CONFIG["APEX_BINARY_ISOLATED_NAMESPACE_LIST"]:
                        try:
                            is_apex_add_success, log_message = add_new_apex_file(aosp_path,
                                                                                 file_path,
                                                                                 lunch_target,
                                                                                 partition_name,
                                                                                 aosp_version)
                        except Exception as e:
                            is_apex_add_success = False
                            log_message = f"Exception occurred: {e}:{traceback.format_exc()}"
                        if not is_apex_add_success:
                            error_message = f"Error adding APEX file: {file_path}|{log_message}"
                        else:
                            error_message = None

                if not error_message:
                    inj_obj, inj_partition = search_and_inject(partition_name, module_type, file_path, target_out_path, aosp_path, lunch_target, aosp_version, apex_merge_file_path_list)
                else:
                    logging.debug(f"File not further processed: {file_path} | {error_message}")
    except Exception as e:
        error_message = f"{e}:{traceback.format_exc()}"
    finally:
        with open(processed_marker, 'w') as marker:
            marker.write("")

    result = error_message, inj_obj, inj_partition
    return result

def rename_file(file_path, new_name):
    """
    Renames a file based on its file path.

    :param file_path: str - The full path to the file.
    :param new_name: str - The new name for the file (without the directory path).
    """
    try:
        directory = os.path.dirname(file_path)
        new_file_path = os.path.join(directory, new_name)
        os.rename(file_path, new_file_path)
        logging.debug(f"File renamed to: {new_file_path}")
        return new_file_path
    except Exception as e:
        logging.error(f"Error renaming file {file_path} to {new_name}: {e}")
        raise






def replace_capex_with_apex(file_path):
    logging.debug(f"APEX file is capex: {file_path}")
    input_folder_path = os.path.dirname(file_path)
    capex_filename = os.path.basename(file_path)
    extracted_apex_file_path = prepare_capex(file_path, input_folder_path,
                                             capex_filename.replace(".capex", ".apex"))
    if extracted_apex_file_path:
        logging.debug(f"APEX file extracted from CAPEX: {extracted_apex_file_path}")
        new_filename = f"{capex_filename}.original_capex"
        rename_file(file_path, new_filename)
        file_path = extracted_apex_file_path
    return file_path


def overwrite_existing_file(target_file_injection_path, file_path):
    try:
        os.remove(target_file_injection_path)
        logging.debug(f"Removed file before indirect injection: {target_file_injection_path}")
        try:
            target_dir = str(os.path.dirname(target_file_injection_path))
            target_filename = str(os.path.basename(target_file_injection_path))
            exact_destination_path = os.path.join(target_dir, target_filename)
            try:
                os.makedirs(target_dir, exist_ok=True)
                schedule_copy(file_path, exact_destination_path)
                #shutil.copy2(file_path, exact_destination_path)
                logging.debug(
                    f"Successfully injected file preserving original name: src:{file_path} -> dst:{exact_destination_path}")
            except Exception as e:
                logging.error(f"Critical error during delete_intermediate_cached_files copy phase: {e}")
        except Exception as e:
            logging.warning(e)
    except Exception as e:
        logging.warning(f"Could not remove file before indirect injection: {target_file_injection_path} | {e}")



def delete_intermediate_cached_files(target_file_injection_path, file_path):
    if os.path.exists(target_file_injection_path):
        overwrite_existing_file(target_file_injection_path, file_path)
    else:
        target_no_vendor_path = remove_vendor_name_from_path(target_file_injection_path)
        logging.debug(f"Removed file before indirect injection (no vendor substring trial): {target_no_vendor_path}")
        if os.path.exists(target_no_vendor_path):
            overwrite_existing_file(target_no_vendor_path, file_path)
        else:
            logging.debug(f"No existing intermediate file found to remove: {target_file_injection_path}")



def find_intermediate_file(aosp_path, md5_original_file):
    """
    Find intermediate files matching the provided MD5.

    If an immutable precomputed mapping (INTERMEDIATE_MD5_MAP) exists it will be used
    for fast lookups. Otherwise, the function will walk the intermediates folder and
    compute MD5s on the fly (legacy fallback).
    """
    global INTERMEDIATE_MD5_MAP

    # Normalize md5 to lowercase for comparison
    target_md5 = (md5_original_file or '').strip().lower()

    # Start timing measurement for this lookup
    start_time = time.time()

    # If a precomputed mapping exists, use it for O(1) lookup
    if INTERMEDIATE_MD5_MAP:
        try:
            matched = INTERMEDIATE_MD5_MAP.get(target_md5, ())
            logging.debug(f"Found intermediate candidates with same md5 hash: {matched}")
            # record timing for the map lookup
            if MEASURE_LOOKUP_PERFORMANCE and ENABLE_INJECTION_PERFORMANCE_LOG:
                try:
                    write_json_nd_output({
                        "function": "find_intermediate_file",
                        "method": "map_lookup",
                        "target_md5": target_md5,
                        "matches": len(matched) if matched is not None else 0,
                        "duration_seconds": time.time() - start_time,
                        "timestamp": time.time()
                    }, PATH_MAPPING_EXECUTION_TIME_LOG)
                except Exception:
                    logging.warning("Could not write find_intermediate_file timing to log")

            # Return a mutable list for compatibility with callers
            return list(matched)
        except Exception:
            logging.warning("Error while using INTERMEDIATE_MD5_MAP, falling back to on-the-fly search")

    logging.debug("Using fallback intermediate file overwrite")

    intermediates_path = str(os.path.join(aosp_path, "out/soong/.intermediates/"))
    matching_intermediate_file_list = []
    if not os.path.exists(intermediates_path):
        logging.debug(f"Intermediates path does not exist: {intermediates_path}")
        # record timing (no files)
        if MEASURE_LOOKUP_PERFORMANCE:
            try:
                write_json_nd_output({
                    "function": "find_intermediate_file",
                    "method": "walk",
                    "target_md5": target_md5,
                    "matches": 0,
                    "duration_seconds": time.time() - start_time,
                    "timestamp": time.time()
                }, PATH_EXECUTION_TIME_LOG)
            except Exception:
                logging.warning("Could not write find_intermediate_file timing to log")
        return matching_intermediate_file_list


    # for root, dirs, files in os.walk(intermediates_path):
    #     for fname in files:
    #         try:
    #             file_path = os.path.join(root, fname)
    #             logging.debug(f"Found intermediate candidate: {file_path}")
    #             if os.path.exists(file_path) and os.path.isfile(file_path):
    #                 md5sum = get_md5_from_file(file_path)
    #                 if not md5sum:
    #                     continue
    #                 if md5sum.strip().lower() == target_md5:
    #                     logging.debug(f"Matched intermediate by md5: {file_path}")
    #                     matching_intermediate_file_list.append(file_path)
    #         except Exception as e:
    #             logging.warning(f"Error while checking intermediate file {fname} in {root}: {e}")
    #             continue
    # record timing for the walk-based search
    if MEASURE_LOOKUP_PERFORMANCE and ENABLE_INJECTION_PERFORMANCE_LOG:
        try:
            write_json_nd_output({
                "function": "find_intermediate_file",
                "method": "walk",
                "target_md5": target_md5,
                "matches": len(matching_intermediate_file_list),
                "duration_seconds": time.time() - start_time,
                "timestamp": time.time()
            }, PATH_MAPPING_EXECUTION_TIME_LOG)
        except Exception:
            logging.warning("Could not write find_intermediate_file timing to log")

    return matching_intermediate_file_list


def build_intermediate_md5_map(aosp_path, intermediates_path, exclude_dirs=None):
    """
    Build a mapping of md5 -> tuple(paths) for all files under intermediates_path.

    :param exclude_dirs: A list of absolute or relative directory paths to skip entirely.
    """
    if exclude_dirs is None:
        exclude_dirs = []

    # Standardize exclusion paths to absolute paths for foolproof comparison
    abs_exclude_dirs = [os.path.abspath(d) for d in exclude_dirs]

    md5_map = defaultdict(list)
    start_time = time.time()
    total_bytes_hashed = 0
    files_hashed = 0
    method = "parallel"

    if not os.path.exists(intermediates_path):
        logging.debug(f"Intermediates path does not exist (build map): {intermediates_path}")
        return {}

    logging.debug(f"Building intermediate md5 map from: {intermediates_path}")

    try:
        cpu = os.cpu_count() or 1
        workers = min(32, max(2, cpu * 2))
    except Exception:
        workers = 4

    futures = {}
    try:
        with Executor(max_workers=workers) as ex:
            for root, dirs, files in os.walk(intermediates_path):
                # --- EXCLUSION LOGIC FOR PARALLEL WALK ---
                for d in list(dirs):
                    full_dir_path = os.path.abspath(os.path.join(root, d))
                    if any(full_dir_path.startswith(ex_dir) for ex_dir in abs_exclude_dirs):
                        dirs.remove(d)
                        logging.debug(f"Skipping excluded folder: {full_dir_path}")
                # -----------------------------------------

                for fname in files:
                    file_path = os.path.join(root, fname)
                    try:
                        # --- SKIP EMPTY FILES ---
                        if os.path.getsize(file_path) == 0:
                            continue
                    except Exception:
                        # If we can't get the size (e.g., broken symlink), skip it
                        continue

                    try:
                        futures[ex.submit(get_md5_from_file, file_path)] = file_path
                    except Exception as e:
                        logging.warning(f"Failed to submit hashing job for {file_path}: {e}")

            # Collect results as they complete
            for fut in as_completed(futures):
                src_path = futures.get(fut)
                try:
                    md5sum = fut.result()
                    if not md5sum:
                        continue
                    md5_key = md5sum.strip().lower()
                    md5_map[md5_key].append(src_path)
                    try:
                        sz = os.path.getsize(src_path)
                        total_bytes_hashed += sz
                    except Exception:
                        sz = 0
                    files_hashed += 1
                except Exception as e:
                    logging.warning(f"Error while hashing intermediate file {src_path}: {e}")
    except Exception:
        logging.warning("Parallel hashing failed, falling back to serial md5 computation")
        method = "serial"
        for root, dirs, files in os.walk(intermediates_path):
            # --- EXCLUSION LOGIC FOR SERIAL WALK ---
            for d in list(dirs):
                full_dir_path = os.path.abspath(os.path.join(root, d))
                if any(full_dir_path.startswith(ex_dir) for ex_dir in abs_exclude_dirs):
                    dirs.remove(d)
            # ---------------------------------------

            for fname in files:
                try:
                    file_path = os.path.join(root, fname)

                    # --- SKIP EMPTY FILES ---
                    if os.path.getsize(file_path) == 0:
                        continue

                    md5sum = get_md5_from_file(file_path)
                    if not md5sum:
                        continue
                    md5_key = md5sum.strip().lower()
                    md5_map[md5_key].append(file_path)
                    try:
                        sz = os.path.getsize(file_path)
                        total_bytes_hashed += sz
                    except Exception:
                        pass
                    files_hashed += 1
                except Exception as e:
                    logging.warning(f"Error while hashing intermediate file {fname} in {root}: {e}")
                    continue

    immutable_map = {k: tuple(v) for k, v in md5_map.items()}
    if ENABLE_INJECTION_PERFORMANCE_LOG:
        try:
            duration = time.time() - start_time
            mb_hashed = total_bytes_hashed / (1024.0 * 1024.0)
            files_per_sec = files_hashed / duration if duration > 0 else None
            mb_per_sec = mb_hashed / duration if duration > 0 else None
            write_json_nd_output({
                "function": "build_intermediate_md5_map",
                "aosp_path": aosp_path,
                "intermediates_path": intermediates_path,
                "method": method,
                "workers": workers,
                "files_hashed": files_hashed,
                "total_bytes_hashed": total_bytes_hashed,
                "duration_seconds": duration,
                "throughput_files_per_second": files_per_sec,
                "throughput_mb_per_second": mb_per_sec,
                "timestamp": time.time()
            }, PATH_MAPPING_EXECUTION_TIME_LOG)
        except Exception:
            logging.debug('Could not write build_intermediate_md5_map timing to log')
    return immutable_map


def is_inject_if_not_exist(target_file_injection_path, file_path):
    if os.path.exists(target_file_injection_path):
        logging.debug(f"Skipped indirect injection for shared library file: {file_path} as "
                     f"ENABLE_SHARED_LIBRARIES_INJECTION_IF_NOT_EXISTS is set.")
        return False
    else:
        logging.debug(f"Allow indirect injection for shared library file: {file_path}")
        return True

def indirect_injection(target_file_injection_path,
                       file_name,
                       target_out_path,
                       partition_name,
                       module_type,
                       file_path,
                       inj_partition,
                       aosp_path,
                       lunch_target,
                       aosp_version):
    start_time = time.time()

    file_ext = os.path.splitext(file_name)[1]
    if file_ext in POST_INJECTOR_CONFIG["SKIPPED_FILE_EXTENSION_LIST_INDIRECT_INJECTION"]:
        logging.debug(f"Skipped indirect injection for file: {file_path} with extension: {file_ext}")
        return None, inj_partition, False

    if POST_INJECTOR_CONFIG.get("ENABLE_BUILD_PROPERTY_MERGER") and file_ext == ".prop" and "build.prop" in file_name:
        logging.debug(f"Skipping indirect injection for property file {file_path} so direct injection property merger can run cleanly.")
        return None, inj_partition, False

    if file_ext == ".xml" and ("/etc/permissions/" in file_path or "/etc/sysconfig/" in file_path):
        logging.debug(f"Skipping indirect injection for /etc XML {file_path} so direct injection additive XML merger can run cleanly.")
        return None, inj_partition, False

    if not (file_name in POST_INJECTOR_CONFIG["ALLOW_FILE_INJECT_ALWAYS"]
            or any(keyword in file_path for keyword in POST_INJECTOR_CONFIG["ALLOW_FILE_INJECT_ALWAYS_KEYWORD_LIST"])):
        if file_ext == ".so" and POST_INJECTOR_CONFIG["ENABLE_SHARED_LIBRARIES_INJECTION_IF_NOT_EXISTS"]:
            is_allowed = is_inject_if_not_exist(target_file_injection_path, file_path)
            if not is_allowed:
                return None, inj_partition, False
        elif module_type == ["EXECUTABLES"] and POST_INJECTOR_CONFIG["ENABLE_EXECUTABLES_INJECTION_IF_NOT_EXISTS"]:
            is_allowed = is_inject_if_not_exist(target_file_injection_path, file_path)
            if not is_allowed:
                return None, inj_partition, False

    if os.path.exists(target_file_injection_path) and not check_file_compatibility(file_path, target_file_injection_path, module_type):
        logging.debug(f"Incompatible file for indirect injection against target: {file_path} | {target_file_injection_path}")
        return None, inj_partition, False

    logging.debug(f"File exists in target path: {target_file_injection_path}. Continue with indirect injection of {file_path}.")
    inj_obj = None
    original_file_path = None
    # Indirect Injection
    if file_name in POST_INJECTOR_CONFIG["INDIRECT_INJECTION_FILE_MAPPING"].keys():
        logging.debug(f"Indirect injection for file with manual mapping: {file_path}")
        if not check_file_compatibility(file_path, target_file_injection_path, module_type):
            logging.debug(f"Incompatible file for indirect injection: {file_path} | {file_name} | {module_type} | {target_file_injection_path}")
        else:
            file_map_entry = POST_INJECTOR_CONFIG["INDIRECT_INJECTION_FILE_MAPPING"][file_name]
            logging.debug(f"Found mapping entry for file: {file_path} | {file_name} | {module_type} | {target_file_injection_path} | mapping: {file_map_entry}")
            if file_map_entry["injection_path"] in file_path:
                original_file_path = os.path.join(target_out_path, file_map_entry["original_path"])
    else:
        original_file_path = search_original_file_in_obj(partition_name,
                                                         module_type,
                                                         file_path,
                                                         file_name,
                                                         target_out_path)

    if original_file_path is None:
        file_path_vendor_replaced = remove_vendor_name_from_path(file_path)
        file_name_vendor_replaced = os.path.basename(file_path_vendor_replaced)
        original_file_path = search_original_file_in_obj(partition_name,
                                                         module_type,
                                                         file_path_vendor_replaced,
                                                         file_name_vendor_replaced,
                                                         target_out_path)

    is_injected = False
    if original_file_path:
        delete_intermediate_cached_files(target_file_injection_path, file_path)
        try:
            logging.debug(f"Original file path type: {type(original_file_path).__name__}: {original_file_path}")
        except Exception:
            logging.debug("Could not determine original_file_path type")
        if isinstance(original_file_path, list):
            original_file_path_list = original_file_path
            logging.debug(f"Indirect injection with multiples files: {original_file_path_list} | {file_path} | {file_name}")
            for overwrite_path in original_file_path_list:
                is_injected = inject_file_into_obj(file_path, overwrite_path, module_type, aosp_path, partition_name, lunch_target, aosp_version)
                inj_obj = (file_path, overwrite_path, module_type)
        else:
            logging.debug(f"Indirect injection with singles file: {original_file_path} | {file_path} | {file_name} ")
            is_injected = inject_file_into_obj(file_path, original_file_path, module_type, aosp_path, partition_name, lunch_target, aosp_version)
            inj_obj = (file_path, original_file_path, module_type)
    else:
        is_injected = False
        error_message = f"Original file not found for indirect injection: {file_path} | {file_name}"
        logging.warning(error_message)
        inj_obj = (error_message, None, module_type)

    # record timing
    if ENABLE_INJECTION_PERFORMANCE_LOG:
        duration = time.time() - start_time
        try:
            write_json_nd_output({
                'function': 'indirect_injection',
                'file': file_path,
                'target_file': target_file_injection_path,
                'method': 'indirect',
                'duration_seconds': duration,
                'timestamp': time.time(),
                'injected': bool(is_injected)
            }, PATH_MAPPING_EXECUTION_TIME_LOG)
        except Exception:
            logging.debug('Could not write indirect_injection timing to log')

    logging.debug(f"Indirect injection result for file: {file_path} | injected: {is_injected} | target_file: {target_file_injection_path}")
    return inj_obj, inj_partition, is_injected


def search_and_inject(partition_name, module_type, file_path, target_out_path, aosp_path, lunch_target, aosp_version, apex_merge_file_path_list=None):
    inj_partition = None
    inj_obj = None
    target_path = None
    is_injected = False
    file_name = os.path.basename(file_path)
    file_extension = os.path.splitext(file_name)[1]

    target_file_injection_path, target_partition_path = get_target_injection_path(file_path, partition_name, target_out_path)
    logging.debug(f"Target file injection path: {target_file_injection_path} ")
    if file_extension == ".apex" or file_extension == ".capex":
        logging.debug(f"APEX Injection Strategy Selection for file: {file_path}")
        if (not os.path.exists(target_file_injection_path)
                and not any(keyword in os.path.basename(target_file_injection_path)
                            for keyword in POST_INJECTOR_CONFIG["ALLOW_APEX_MERGE_KEYWORD_LIST"])):
            # Direct Injection - Repacked APEX Files
            target_path, is_injected = inject_file_into_partition(file_path, target_file_injection_path, aosp_path, partition_name, lunch_target, aosp_version)
            inj_partition = (file_path, target_path, module_type)
            if is_injected:
                logging.debug(f"Direct injection complete for: {file_path}")
            else:
                logging.debug(f"Direct injection did not inject for: {file_path}")
        else:
            # Indirect Injection for Merged APEX Files
            inj_obj, inj_partition, is_injected = indirect_injection(target_file_injection_path, file_name, target_out_path,
                                                        partition_name, module_type, file_path, inj_partition, aosp_path, lunch_target, aosp_version)
            if is_injected and apex_merge_file_path_list and len(apex_merge_file_path_list) > 0:
                if POST_INJECTOR_CONFIG["ENABLE_APEX_INTERMEDIATE_OVERWRITE"]:
                    inject_apex_intermediate_files(file_name, file_path, apex_merge_file_path_list, partition_name, target_out_path, aosp_path, lunch_target, aosp_version)


    elif not os.path.exists(target_file_injection_path):
        # Direct Injection
        target_path, is_injected = inject_file_into_partition(file_path, target_file_injection_path, aosp_path, partition_name, lunch_target, aosp_version)
        inj_partition = (file_path, target_path, module_type)
        if is_injected:
            logging.debug(f"Direct injection complete for: {file_path}")
        else:
            logging.warning(f"Direct injection did not inject file, not fallback to indirect injection: {file_path}")
    else:
        inj_obj, inj_partition, is_injected = indirect_injection(target_file_injection_path, file_name, target_out_path,
                                                    partition_name, module_type, file_path, inj_partition, aosp_path, lunch_target, aosp_version)
        if is_injected:
            logging.debug(f"Indirect injection complete for file: {file_path}")

        if not is_injected and is_injected is not None:
            logging.debug(f"Fallback to Direct injection: {file_path}")
            # Fallback to Direct Injection
            target_path, is_injected = inject_file_into_partition(file_path, target_file_injection_path, aosp_path, partition_name, lunch_target, aosp_version)
            inj_partition = (file_path, target_path, module_type)
            if is_injected:
                logging.debug(f"Direct injection complete for file: {file_path}")
            else:
                logging.warning(f"Direct+Indirect injection complete for file but file not injected: {file_path}")


    # TODO FIX THIS
    if file_name in POST_INJECTOR_CONFIG["COPY_TO_SPECIFIC_PATH"]:
        dst_path = os.path.join(target_partition_path, POST_INJECTOR_CONFIG["COPY_TO_SPECIFIC_PATH"][file_name])
        if not os.path.exists(dst_path):
            if file_name == "MitypeVF.ttf":
                test_data = "/system/fonts/Roboto-Regular.ttf"
                try:
                    with open(dst_path, "w") as f:
                        f.write(test_data)
                    logging.debug(f"COPY_TO_SPECIFIC_PATH: {file_path} to {dst_path}")
                except Exception as e:
                    logging.error(f"Error copying file for COPY_TO_SPECIFIC_PATH: {file_path} to {dst_path} | {e}")
            else:
                schedule_copy(file_path, dst_path)
        else:
            logging.debug(f"SKIPPED COPY_TO_SPECIFIC_PATH: {file_path}|{dst_path} already exists")

    # Try to avoid linking errors
    if file_extension in [".so"] and "/apex/lib64/" in file_path:
        dst_path = os.path.join(target_partition_path, "lib64", os.path.basename(file_path))
        logging.debug(f"Matched outside Library: {file_path}: {dst_path}")
        if not os.path.exists(dst_path):
            logging.debug(f"COPY_OUTSIDE_LIB_TO_LIB64: {file_path} to {dst_path}")
            schedule_copy(file_path, dst_path)

            if "android.hardware.light-V1-ndk_platform.so" in file_path:
                dst_path2 = dst_path.replace("/system_ext/", "/vendor/")
                if not os.path.exists(dst_path2):
                    logging.debug(f"COPY android.hardware.light-V1-ndk_platform.so: {file_path} to {dst_path2}")
                    schedule_copy(file_path, dst_path2)
        else:
            logging.debug(f"Outside Lib match SKIPPED (file already exists): {file_path}|{dst_path}")

    if target_path:
        try:
            md5sum = hashlib.md5(target_path).hexdigest()
            inj_partition = (inj_partition[0], inj_partition[1], inj_partition[2], md5sum)
        except Exception:
            pass

    return inj_obj, inj_partition


def inject_apex_intermediate_files(apex_file_name, file_path, apex_merge_file_path_list, partition_name, target_out_path,
                                   aosp_path, lunch_target, aosp_version):
    """
    Injects apex intermediate files (libs, binaries, etc.) that are found within the apex image.
    """
    tag = "apex_intermediata_matching"
    for apex_sub_file_path in apex_merge_file_path_list:
        if "/lib/" in apex_sub_file_path:
            logging.debug(f"{tag}: Skipping apex intermediate file indirect injection because of /lib/ (32-bit) in path: {apex_sub_file_path}")
            continue
        is_injected = False
        md5 = get_md5_from_file(apex_sub_file_path)
        logging.debug(f"{tag}: Injecting apex intermediate file: {apex_sub_file_path}:{md5}|{apex_file_name}")
        try:
            module_type = "MISC"
            sub_file_name = os.path.basename(apex_sub_file_path)
            apex_filename_no_ext = str(os.path.splitext(apex_file_name)[0])
            logging.debug(f"{tag}: Injecting apex intermediate file: {apex_sub_file_path}")

            original_file_path = search_original_file_in_obj(partition_name,
                                                             module_type,
                                                             apex_sub_file_path,
                                                             sub_file_name,
                                                             target_out_path,
                                                             replace_intermediate=f".{apex_filename_no_ext}_intermediates",
                                                             must_contain=apex_filename_no_ext)

            if original_file_path is None:
                logging.debug(
                    f"{tag}: Did not find original file path for apex intermediate file: {apex_sub_file_path}|{original_file_path}|{apex_file_name}. Searching without vendor sub-strings.")
                apex_sub_file_path_vendor_replaced = remove_vendor_name_from_path(apex_sub_file_path)
                file_name_vendor_replaced = os.path.basename(apex_sub_file_path_vendor_replaced)
                original_file_path = search_original_file_in_obj(partition_name,
                                                                 module_type,
                                                                 apex_sub_file_path_vendor_replaced,
                                                                 file_name_vendor_replaced,
                                                                 target_out_path,
                                                                 replace_intermediate=f".{apex_filename_no_ext}_intermediates",
                                                                 must_contain=apex_filename_no_ext)

            if original_file_path:
                logging.debug(
                    f"{tag}: Found original file path for apex intermediate file: {apex_sub_file_path}|{original_file_path}|{apex_file_name}")

                if isinstance(original_file_path, list):
                    any_candidate_succeeded = False
                    for file_path_candidate in original_file_path:
                        logging.debug(
                            f"Found candidate file path for apex intermediate file: {apex_sub_file_path}|{file_path_candidate}|{apex_file_name}")

                        success = inject_file_into_obj(apex_sub_file_path,
                                                       file_path_candidate,
                                                       module_type,
                                                       aosp_path,
                                                       partition_name,
                                                       lunch_target,
                                                       aosp_version)
                        if success:
                            any_candidate_succeeded = True
                    is_injected = any_candidate_succeeded
                else:
                    logging.debug(
                        f"{tag}: Inject file path for apex intermediate file: {apex_sub_file_path}|{original_file_path}|{apex_file_name}")
                    is_injected = inject_file_into_obj(apex_sub_file_path,
                                                       original_file_path,
                                                       module_type,
                                                       aosp_path,
                                                       partition_name,
                                                       lunch_target,
                                                       aosp_version)
            else:
                is_injected = False

            if is_injected:
                logging.debug(
                    f"{tag}: Injected apex intermediate file Indirect: {original_file_path}|{is_injected}|{apex_sub_file_path}|{file_path}|{apex_filename_no_ext}")
            else:
                logging.warning(f"{tag}: APEX Intermediate: Indirect injection failed for file: {original_file_path}|{is_injected}|{apex_sub_file_path}|{file_path}|{apex_filename_no_ext}")

        except Exception as e:
            logging.error(f"{tag}: Error processing file: {file_path}|{e}")
            traceback.print_exc()


def cleanup_files(directory):
    """
    Remove all .lock and .processed files in the given directory and its subdirectories.

    :param directory: str - path to the directory to clean up.
    """
    logging.debug(f"Cleaning up all .lock files in {directory}")
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith('.fmd-aecs-lock') or file.endswith('.fmd-aecs-processed'):
                file_path = os.path.join(root, file)
                try:
                    os.remove(file_path)
                    #logging.debug(f"Removed file: {file_path}")
                except Exception as e:
                    logging.error(f"Error removing file {file_path}: {e}")


def process_partition_files(aosp_path, folder_path, target_out_path, executor, lunch_target, pre_injector_package_list, firmware_id, cookies, aosp_version):
    logging.debug(f"Processing {folder_path} into {target_out_path}")
    logging.debug(f"AOSP Path: {aosp_path} "
                  f"| Target Out Path: {target_out_path} "
                  f"| Lunch Target: {lunch_target} "
                  f"| Folder Path: {folder_path} "
                  f"| Pre Injector Package List: {len(pre_injector_package_list)}")
    error_list = []
    inj_obj_list = []
    inj_partition_list = []
    processed_files = set()
    partition_name = os.path.basename(folder_path)
    partition_path = folder_path

    file_paths = list(set(os.path.join(root, file_name.strip()) for root, _, file_name_list in scandir_walk(folder_path)
                          for file_name in file_name_list))
    logging.debug(f"Found {len(file_paths)} files in {folder_path} for post-injection...")

    # Initialize tqdm progress bar
    progress_bar = tqdm(total=len(file_paths), desc=f"Processing files in partition: {partition_name}")

    vintf_path_keywords = [
        "/etc/vintf/"
    ]
    vintf_path_list = []
    future_dict = {}
    skip_counter = 0
    for file_path in file_paths:
        with processed_files_lock:
            if file_path in processed_files:
                skip_counter += 1
                continue
            for keyword in vintf_path_keywords:
                if keyword in file_path:
                    vintf_path_list.append(file_path)
                    skip_counter += 1
                    continue
            processed_files.add(file_path)

        logging.debug(f"Submitting file for injection: {file_path} | Partition: {partition_name} "
                     f"| Target Out Path: {target_out_path} | Lunch Target: {lunch_target} | Length of pre_injector_package_list: {len(pre_injector_package_list)}")
        future = executor.submit(process_file_concurrently, aosp_path, file_path, partition_name, target_out_path, lunch_target, pre_injector_package_list, firmware_id, cookies, aosp_version)
        future_dict[future] = file_path

    logging.debug(f"Finished processing {len(processed_files)}/{len(file_paths)} files in partition: {partition_name}. "
                 f"Skipped {skip_counter} files that were already processed.")

    for future in as_completed(future_dict):
        file_path = future_dict[future]
        try:
            result = future.result()
            if result[0]:  # If there's an error
                error_list.append(result[0])
            if result[1]:  # Indirect Injection
                inj_obj_list.append(result[1])
            if result[2]:  # Direct Injection
                inj_partition_list.append(result[2])
        except Exception as exc:
            logging.error(f"Error processing file {file_path}: {exc}")
            error_list.append(str(exc))
        finally:
            if future.exception():
                logging.error(f"Future for file {file_path} raised an exception: {future.exception()}")
            progress_bar.update(1)

    if POST_INJECTOR_CONFIG["ENABLE_SEMANTIC_INJECTOR"]:
        logging.debug(f"ENABLE_SEMANTIC_INJECTOR is activated: {SEMANTIC_INJECTOR_LOG_FILE_PATH}")
        if POST_INJECTOR_CONFIG["ENABLE_VINTF_MERGE"]:
            if vintf_path_list and len(inj_obj_list) > 0:
                logging.debug(f"Starting VINTF Merge")
                try:
                    handle_vintf_merge(aosp_path, aosp_version, str(partition_name), vintf_path_list)
                except Exception as e:
                    logging.error(e)

        if POST_INJECTOR_CONFIG["ENABLE_SELINUX_MERGER"]:
            logging.debug(f"Starting SELinux Policy Merging for partition: {partition_name}")
            if partition_name == "system":
                try:
                    handle_seplicy_merging(aosp_version, aosp_path, partition_path)
                except Exception as e:
                    logging.error(e)



    progress_bar.close()
    #handle_duplicated_permissions(target_out_path)
    cleanup_files(folder_path)
    # Wait for any scheduled non-blocking copy tasks to finish before moving on
    try:
        wait_for_all_copy_tasks()
    except Exception as e:
        logging.warning(f"Error waiting for copy tasks to finish: {e}")



    return error_list, inj_obj_list, inj_partition_list


def scandir_walk(top):
    """
    Highly optimized, iterative generator yielding (dirpath, dirnames, filenames).
    """
    stack = [top]

    while stack:
        dir_path = stack.pop()
        dirnames = []
        filenames = []
        dir_paths = []

        d_append = dirnames.append
        dp_append = dir_paths.append
        f_append = filenames.append

        try:
            with os.scandir(dir_path) as scandir_it:
                for entry in scandir_it:
                    if entry.is_dir(follow_symlinks=False):
                        d_append(entry.name)
                        dp_append(entry.path)
                    else:
                        f_append(entry.name)
        except OSError:
            # Broader than PermissionError: catches FileNotFoundError
            # (broken symlinks) and NotADirectoryError safely.
            continue

        yield dir_path, dirnames, filenames
        stack.extend(dir_paths)





def is_abi_compatible(candidate_path, file_path):
    candidate_arch = check_binary_architecture(candidate_path)
    src_arch = check_binary_architecture(file_path)
    logging.debug(f"Checking {candidate_path}|{candidate_arch}|{file_path}|{src_arch}")
    if candidate_arch == "Unknown architecture" or candidate_arch != src_arch:
        logging.debug(f"Skipping {candidate_path}|{candidate_arch}|{file_path} "
                      f"due to architecture mismatch")
        is_same_architecture = False
    else:
        is_same_architecture = True
    return is_same_architecture

def is_parent_dir_arm_and_target_arm(file_path, candidate_path):
    """
    Prevent matching of arm to arm64 and vice versa when both paths reside in explicit arm/arm64 subdirectories.
    """
    parent_dir_file_path = os.path.basename(os.path.dirname(file_path))
    parent_dir_candidate = os.path.basename(os.path.dirname(candidate_path))

    if parent_dir_file_path not in ("arm", "arm64") or ("arm" not in parent_dir_candidate):
        return True

    is_match = False
    if parent_dir_file_path == "arm64":
        if "arm64" in parent_dir_candidate:
            is_match = True
    elif parent_dir_file_path == "arm":
        if "arm64" not in parent_dir_candidate and "arm" in parent_dir_candidate:
            is_match = True

    logging.debug(f"Checking parent dir: {parent_dir_file_path}|{parent_dir_candidate} for {file_path}|{candidate_path}: "
                 f"result: {is_match}")
    return is_match


def get_all_files(directory):
    all_files = []
    for root, _, files in os.walk(directory):
        for file in files:
            all_files.append(os.path.join(root, file))
    return all_files


def verify_direct_injection_targets(inj_partition_list):
    """
    Verify that all Direct Injection target paths exist on the filesystem.

    :param inj_partition_list: list of tuples (source_path, target_path, module_type)
    :return: list of error message strings for missing targets
    """
    errors = []
    if not inj_partition_list:
        return errors

    for obj in inj_partition_list:
        try:
            # Expect tuples like: (source, target, module_type[, ...])
            if not isinstance(obj, tuple) or len(obj) < 2:
                logging.debug(f"Skipping malformed Direct Inject entry (not a tuple or too short): {obj}")
                continue
            source_path = obj[0]
            target_path = obj[1]
            module_type = obj[2] if len(obj) > 2 else None

            if not target_path or not os.path.exists(target_path):
                msg = f"Sanity Check: Direct Injection target missing (after injection). Likely file removed by the AOSP build system): {target_path} for source {source_path} | module_type: {module_type}"
                logging.error(msg)
                errors.append(msg)
        except Exception as e:
            logging.error(f"Error while verifying Direct Inject entry {obj}: {e}")
            errors.append(f"Error while verifying Direct Inject entry {obj}: {e}")

    return errors


def search_original_file_in_obj(partition_name,
                                module_type,
                                file_path,
                                file_name,
                                target_out_path,
                                replace_intermediate="_intermediates",
                                exact_match_files=True,
                                must_contain=""):
    """
    Searches for the original file in the AOSP source code.

    :param partition_name: str - name of the partition.
    :param module_type: str - type of the module.
    :param file_path: str - path to the file which needs to be injected.
    :param target_out_path: str - path to the AOSP target out folder.
    :param file_name: str - name of the file to search for.

    :return: str - path to the original file.


    """
    target_obj_path = os.path.join(target_out_path, FOLDER_NAME_OBJECTS)
    if not target_obj_path.endswith("/"):
        target_obj_path += "/"

    if module_type not in ["MISC", "STATIC_CONFIG"]:
        search_folder_path = str(target_obj_path) + module_type
    else:
        search_folder_path = str(target_obj_path)

    if partition_name in ["super", "system"]:
        partition_name = ""

    result_file_path_list = []
    # instrumentation
    start_time = time.time()
    candidates_examined = 0
    matches_found = 0
    lookup_method = "unknown"
    # Try to use pre-built basename -> paths index for the target_obj_path to avoid repeated os.walk
    idx = None
    try:
        # FILE_INDEX_CACHE stores indexes keyed by the base target_obj_path
        base_obj_path = os.path.join(target_out_path, FOLDER_NAME_OBJECTS)
        idx = FILE_INDEX_CACHE.get(base_obj_path)
    except Exception:
        idx = None

    if idx is not None:
        # Gather candidate file paths for this basename
        file_path_list = list(idx.get(file_name, []))
        lookup_method = "index"
        if exact_match_files and not file_path_list:
            # No exact basename matches: filter by extension across the whole index
            file_extension = os.path.splitext(file_name)[1]
            # flatten all paths with the extension
            file_path_list = [p for paths in idx.values() for p in paths if os.path.splitext(p)[1] == file_extension]
            logging.debug("File Matcher: No exact matches found for %s. Filtered by extension (%s) using index", file_name, file_extension)
    else:
        # Fallback: expensive full walk
        file_path_list = get_all_files(search_folder_path)
        lookup_method = "walk"
        file_name_list = [os.path.basename(file) for file in file_path_list]
        if exact_match_files:
            matches = [file_path_list[i] for i, name in enumerate(file_name_list) if name == file_name]
            if matches:
                for matching_file in matches[:]:
                    if not check_file_compatibility(file_path, matching_file, module_type):
                        logging.debug("File Matcher: File not compatible: %s|%s", file_path, matching_file)
                        matches.remove(matching_file)
                logging.debug("File Matcher: Found exact matches for %s: %s", file_name, matches)
                file_path_list = matches
            else:
                file_extension = os.path.splitext(file_name)[1]
                file_path_list = [file for file in file_path_list if os.path.splitext(file)[1] == file_extension]
                logging.debug("File Matcher: No exact matches found for %s. Filtered by extension (%s)", file_name, file_extension)
    duration_first_part = time.time() - start_time

    module_name = os.path.splitext(file_name)[0]
    logging.debug("File Matcher:%s Searching in %s for module_name: %s and file_name: %s", module_type, search_folder_path, module_name, file_name)

    contains_vndk = "vndk" in file_path
    contains_ndk = "ndk" in file_path
    root_cache = {}
    for file in file_path_list:
        candidates_examined += 1
        candidate_path = file
        candidate_file_name = os.path.basename(candidate_path)
        # Strip the root folder name to match the module name
        root = os.path.dirname(file)
        if root not in root_cache:
            # Perform the expensive stripping logic only once per unique directory
            stripped = root.replace(replace_intermediate, "")
            stripped = stripped.replace(f"_{partition_name}", "")
            stripped = stripped.replace("v1_prebuilt", "")
            root_cache[root] = os.path.basename(stripped)

        root_folder_name_stripped = root_cache[root]
        logging.debug("File Matcher: %s:%s - Root Folder Name stripped: %s", module_name, file_name, root_folder_name_stripped)

        if ("vndk" in candidate_path and not contains_vndk) or ("ndk" in candidate_path and not contains_ndk):
            logging.debug("File Matcher: VNDK/NDK Rule enforced -> Candidate: %s has vndk but target not: %s", candidate_path, file_path)
            continue

        if not check_file_compatibility(file_path, candidate_path, module_type):
            logging.debug("File Matcher: File not compatible: %s|%s", file_path, candidate_path)
            continue

        # Check if there is an exact match for the file name
        if candidate_file_name == file_name:
            logging.debug("File Matcher test exact match:%s Found %s in %s", file_name, candidate_path, root)

            # Verify if it matches the partition criteria
            if not partition_name or partition_name in root:
                logging.debug("File Matcher: Found file that matches partition: %s, candidate_path: %s", file_name, candidate_path)

                if "com.android" in candidate_path and "com.android" not in file_path:
                    logging.debug("File Matcher: com.android rule enforced -> Candidate: %s has com.android but target not: %s", candidate_path, file_path)
                    continue

                logging.debug("File Matcher: File found via direct match: %s|%s", file_path, candidate_path)
                result_file_path = candidate_path
                result_file_path_list.append(result_file_path)
                matches_found += 1
        # Check if the folder has the same name but the file within the folder is named differently
        elif module_name == root_folder_name_stripped and partition_name in root:
            logging.debug("File Matcher: Found module name: %s:%s:%s with partition %s", module_name, file_name, root, partition_name)
            file_extension_src = os.path.splitext(file_name)[1]
            file_extension_obj = os.path.splitext(file)[1]

            if file_extension_src.lower().strip() == file_extension_obj.lower().strip():
                logging.debug("File Matcher: Found file: %s, candidate_path: %s", file_name, candidate_path)
                logging.debug("File Matcher: File found via module name: %s|%s", file_path, candidate_path)
                result_file_path = candidate_path
                result_file_path_list.append(result_file_path)
            elif ((file_extension_src == ".apex" and file_extension_obj == ".capex")
                  or (file_extension_src == ".capex" and file_extension_obj == ".apex")):
                # Matching apex to capex files
                if POST_INJECTOR_CONFIG["ALLOW_APEX_INJECTION_MERGE"]:
                    result_file_path = candidate_path
                    logging.debug("File Matcher: Found APEX file2: %s, result_file_path: %s", file_name, result_file_path)
                    result_file_path_list.append(result_file_path)
                    matches_found += 1
        else:
            logging.debug("File Matcher: File not found: %s", file_name)

    duration_second_part = time.time() - start_time

    # record timing and counts
    if ENABLE_INJECTION_PERFORMANCE_LOG:
        try:
            duration = time.time() - start_time
            write_json_nd_output({
                "function": "search_original_file_in_obj",
                "target_out_path": target_out_path,
                "search_folder_path": search_folder_path,
                "partition_name": partition_name,
                "module_type": module_type,
                "file_name": file_name,
                "lookup_method": lookup_method,
                "candidates_examined": candidates_examined,
                "matches_found": matches_found,
                "duration_seconds": duration,
                "duration_seconds_first_part": duration_first_part,
                "duration_seconds_second_part": duration_second_part,
                "timestamp": time.time()
            }, PATH_MAPPING_EXECUTION_TIME_LOG)
        except Exception:
            logging.debug('Could not write search_original_file_in_obj timing to log')

    not_contain = "/obj/FAKE/"
    result_file_path_list = [path for path in result_file_path_list if not_contain not in path]
    result_file_path_list = [path for path in result_file_path_list if must_contain in path]

    if len(result_file_path_list) > 0:
        logging.debug("File Matcher: Found file for %s in %s with partition %s and results: %s", file_name, search_folder_path, partition_name, result_file_path_list)
        return result_file_path_list
    else:
        logging.debug("File Matcher: No file found for %s in %s with partition %s", file_name, search_folder_path, partition_name)
        return None


def check_file_compatibility(file_path, candidate_path, module_type):
    # Start high-resolution timer for compatibility check
    start_tc = time.perf_counter()
    is_match = True
    logging.debug(f"check_file_compatibility: {module_type}:{file_path}|{candidate_path}")

    if is_elf_binary(file_path):
        logging.debug(f"File Matcher: Checking ELF compatibility for {file_path}|{candidate_path}")
        candidate_arch = check_shared_object_architecture(candidate_path)
        file_arch = check_shared_object_architecture(file_path)
        logging.debug(f"File Matcher: ARCH {candidate_arch}|{file_arch} | {module_type}:{file_path}|{candidate_path}")
        if not is_abi_compatible(candidate_path, file_path):
            logging.debug(f"File Matcher: ABI not compatible: {file_path}|{candidate_path}")
            is_match = False

        if candidate_arch != file_arch:
            logging.debug(f"File Matcher: Shared object architecture not the same: {file_path}|{candidate_path}")
            is_match = False
        else:
            logging.debug(f"File Matcher: Shared object architecture is the same: {file_path}|{candidate_path}")

    if ("/lib/" in file_path and "/lib64/" in candidate_path) or ("/lib64/" in file_path and "/lib/" in candidate_path):
        logging.debug(f"File Matcher: lib vs lib64 directory mismatch: {file_path}|{candidate_path}")
        is_match = False

    if os.path.basename(os.path.dirname(file_path)) in ("arm", "arm64"):
        if not is_parent_dir_arm_and_target_arm(file_path, candidate_path):
            logging.debug(f"File Matcher: Parent dir arm/arm64 mismatch: {file_path}|{candidate_path}")
            is_match = False

    if module_type == "JAVA_LIBRARIES":
        is_match = True


    if ENABLE_INJECTION_PERFORMANCE_LOG:
        # record timing for check_file_compatibility
        try:
            duration = time.perf_counter() - start_tc
            write_json_nd_output({
                "function": "check_file_compatibility",
                "module_type": module_type,
                "file_path": file_path,
                "candidate_path": candidate_path,
                "is_match": bool(is_match),
                "duration_seconds": duration,
                "timestamp": time.time()
            }, PATH_MAPPING_EXECUTION_TIME_LOG)
        except Exception:
            logging.debug('Could not write check_file_compatibility timing to log')

    return is_match


def is_top_folder(library_path, folder_name):
    """
    Check if the library path is the top folder.

    :param library_path: str - path to the library.
    :param folder_name: str - name of the top folder.

    :return: bool - True if the library path is the top folder, False otherwise.

    """
    path_list = library_path.split(os.sep)
    return path_list[0] == folder_name


def get_subfolders(file_path, top_folder_name):
    """
    Get the subfolders after a specific top folder.

    :param file_path: str - path to the file.
    :param top_folder_name: str - name of the top folder.

    :return: list(str) - list of subfolders after the folder in case there are any subfolders.

    """
    subfolders = []
    if top_folder_name in file_path and not is_top_folder(file_path, top_folder_name):
        path_list = file_path.split(os.sep)
        top_folder_index = path_list.index(top_folder_name.replace("/", ""))
        subfolders = path_list[top_folder_index + 1:]
        subfolders = subfolders[:-1]
    return subfolders


def set_executable_permission(file_path):
    """
    Set the executable permission for a file.

    :param file_path: str - path to the file.
    :return: bool - True if the permission was set successfully, False otherwise.
    """
    try:
        file_extension = os.path.splitext(file_path)[1]
        if os.path.exists(file_path) \
            and not os.path.islink(file_path) \
            and os.path.isfile(file_path) \
            and (file_extension is None or file_extension == ".so"):
                os.chmod(file_path, os.stat(file_path).st_mode | stat.S_IEXEC)
        logging.debug(f"Set executable permission for file: {file_path}")
        return True
    except Exception as e:
        logging.warning(f"{e}")
        return False

def get_target_injection_path(source_file_path, partition_name, target_out_path):
    mapped_partition = OEM_CUSTOM_PARTITION_MAP.get(partition_name, partition_name)

    filename = os.path.basename(source_file_path)
    file_extension = os.path.splitext(source_file_path)[1]

    target_partition_path = os.path.join(target_out_path, mapped_partition)
    if not target_partition_path.endswith("/"):
        target_partition_path += "/"
    subfolder_list = get_subfolders(source_file_path, partition_name)
    # Strip redundant leading folder if the extracted partition had a nested folder with its own name (e.g. my_product/my_product/...)
    if subfolder_list and subfolder_list[0] == partition_name:
        subfolder_list = subfolder_list[1:]
    if len(subfolder_list) == 0:
        target_dir_injection_path = target_partition_path
    else:
        # Adjust path for AOSP build directory structure
        if (file_extension in [".so", ".1", ".2", ".3", ".4", ".5", ".6", ".7", ".8", ".9"]
                and POST_INJECTOR_CONFIG["USE_ISOLATED_NAMESPACE"]
                and "/lib" in source_file_path
                and check_shared_object_architecture(source_file_path) == "64-bit"
                and filename in POST_INJECTOR_CONFIG["ISOLATED_NAMESPACE_NATIVE_LIBRARY_LIST"]):
            logging.debug(f"File uses isolated namespace: {filename}|{source_file_path}")
            target_dir_injection_path = os.path.join(target_partition_path, "fmd")
        else:
            target_dir_injection_path = target_partition_path + str(os.path.join(*subfolder_list))

        if "/system/" in target_dir_injection_path:
            if "/system/system/" in target_dir_injection_path:
                target_dir_injection_path = target_dir_injection_path.replace("/system/system/", "/system/")
            elif "/system/system_ext/" in target_dir_injection_path:
                target_dir_injection_path = target_dir_injection_path.replace("/system/system_ext/", "/system_ext/")
            elif "/system/vendor/" in target_dir_injection_path:
                target_dir_injection_path = target_dir_injection_path.replace("/system/vendor/", "/vendor/")
            elif "/system/product/" in target_dir_injection_path:
                target_dir_injection_path = target_dir_injection_path.replace("/system/product/", "/product/")

    if (not os.path.exists(target_dir_injection_path)
            and not os.path.islink(target_dir_injection_path)):
        logging.debug(f"Creating directory: {target_dir_injection_path}")
        os.makedirs(target_dir_injection_path, exist_ok=True)

    target_file_injection_path = os.path.join(target_dir_injection_path, os.path.basename(source_file_path))
    target_file_injection_path = os.path.normpath(target_file_injection_path)
    return target_file_injection_path, target_partition_path



def _strip_unsafe_hardware_features(root_elem):
    """
    Removes <feature> / <unavailable-feature> elements that declare physical hardware HALs
    absent in the Goldfish/Ranchu emulator (e.g. fingerprint, biometrics, strongbox, vulkan, uwb, nfc).
    Returns the number of removed elements.
    """
    removed_count = 0
    for child in list(root_elem):
        tag = child.tag.split("}")[-1]
        if tag in ("feature", "unavailable-feature"):
            feat_name = child.attrib.get("name", "")
            if any(feat_name.startswith(prefix) for prefix in UNSAFE_HARDWARE_FEATURES):
                root_elem.remove(child)
                removed_count += 1
    return removed_count


def sanitize_and_merge_etc_xml(source_xml_path, target_xml_path=None):
    """
    Sanitizes a vendor /etc/permissions/*.xml or /etc/sysconfig/*.xml file by stripping
    incompatible physical-hardware <feature> declarations, and if target_xml_path already
    exists in AOSP (e.g. /system/etc/permissions/platform.xml), additively merges vendor
    <permission>, <assign-permission>, <library>, and <privapp-permissions> entries into
    AOSP's XML tree without deleting AOSP's existing entries.
    Returns path to the sanitized/merged XML file.
    """
    if not source_xml_path or not os.path.exists(source_xml_path):
        return source_xml_path

    try:
        vendor_tree = ET.parse(source_xml_path)
        vendor_root = vendor_tree.getroot()
    except Exception as e:
        logging.debug(f"Could not parse XML {source_xml_path} for sanitization/merge: {e}")
        return source_xml_path

    _strip_unsafe_hardware_features(vendor_root)

    if target_xml_path and os.path.exists(target_xml_path) and os.path.abspath(target_xml_path) != os.path.abspath(source_xml_path):
        try:
            aosp_tree = ET.parse(target_xml_path)
            aosp_root = aosp_tree.getroot()
            _strip_unsafe_hardware_features(aosp_root)

            # Index existing AOSP elements for additive deduplicated merge
            existing_privapp = {
                el.attrib.get("package"): el
                for el in aosp_root
                if el.tag.split("}")[-1] == "privapp-permissions" and el.attrib.get("package")
            }
            existing_permissions = {
                el.attrib.get("name"): el
                for el in aosp_root
                if el.tag.split("}")[-1] == "permission" and el.attrib.get("name")
            }
            existing_generic = {
                (el.tag.split("}")[-1], tuple(sorted(el.attrib.items())))
                for el in aosp_root
            }

            for v_el in list(vendor_root):
                v_tag = v_el.tag.split("}")[-1]
                if v_tag == "privapp-permissions" and v_el.attrib.get("package"):
                    pkg = v_el.attrib["package"]
                    if pkg in existing_privapp:
                        target_el = existing_privapp[pkg]
                        existing_sub = {
                            (sub.tag.split("}")[-1], sub.attrib.get("name"))
                            for sub in target_el
                        }
                        for sub in v_el:
                            key = (sub.tag.split("}")[-1], sub.attrib.get("name"))
                            if key not in existing_sub:
                                target_el.append(sub)
                                existing_sub.add(key)
                    else:
                        aosp_root.append(v_el)
                        existing_privapp[pkg] = v_el
                elif v_tag == "permission" and v_el.attrib.get("name"):
                    perm_name = v_el.attrib["name"]
                    if perm_name in existing_permissions:
                        target_el = existing_permissions[perm_name]
                        existing_gids = {
                            sub.attrib.get("gid")
                            for sub in target_el
                            if sub.tag.split("}")[-1] == "group"
                        }
                        for sub in v_el:
                            if sub.tag.split("}")[-1] == "group" and sub.attrib.get("gid") not in existing_gids:
                                target_el.append(sub)
                                existing_gids.add(sub.attrib.get("gid"))
                    else:
                        aosp_root.append(v_el)
                        existing_permissions[perm_name] = v_el
                else:
                    sig = (v_tag, tuple(sorted(v_el.attrib.items())))
                    if sig not in existing_generic:
                        aosp_root.append(v_el)
                        existing_generic.add(sig)

            out_tree = aosp_tree
        except Exception as e:
            logging.warning(f"Failed to merge vendor XML {source_xml_path} with {target_xml_path}: {e}")
            out_tree = vendor_tree
    else:
        out_tree = vendor_tree

    try:
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".xml", delete=False) as tf:
            out_path = tf.name
        out_tree.write(out_path, encoding="utf-8", xml_declaration=True)
        return out_path
    except Exception as e:
        logging.warning(f"Failed to write sanitized/merged XML for {source_xml_path}: {e}")
        return source_xml_path


def run_build_property_merger(source_file_path, target_file_injection_path):
    filename = os.path.basename(source_file_path)
    if not os.path.exists(PATH_PROPERTY_MERGE_CONFLICTS_DIR):
        os.makedirs(PATH_PROPERTY_MERGE_CONFLICTS_DIR, exist_ok=True)
    conflicts_out_file_path = os.path.join(PATH_PROPERTY_MERGE_CONFLICTS_DIR, f"{filename}.conflicts")
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".txt", delete=False) as temp_file:
        pass
    merged_prop_file_path = temp_file.name
    try:
        if target_file_injection_path == source_file_path:
            logging.warning(f"Source and target property files are the same: {source_file_path}. Skipping merge.")
            return source_file_path
        if not os.path.exists(target_file_injection_path):
            logging.debug(f"Target property file {target_file_injection_path} does not exist yet; filtering vendor properties into new file.")
        start_property_merge(target_file_injection_path, source_file_path, merged_prop_file_path, conflicts_out_file_path)
        if os.path.exists(merged_prop_file_path) and os.path.getsize(merged_prop_file_path) > 0:
            logging.debug(f"Success: Created merged property file {merged_prop_file_path} for {target_file_injection_path}")
            return merged_prop_file_path
    except Exception as e:
        logging.error(f"An error occurred during merging build properties: {e}")
    return source_file_path


# Direct Injection
def inject_file_into_partition(source_file_path, target_file_injection_path, aosp_path, partition_name, lunch_target, aosp_version):
    is_injected = False
    start_time = time.time()
    filename = os.path.basename(target_file_injection_path)
    if POST_INJECTOR_CONFIG["OVERWRITE_APP_PROCESS_32"]:
        source_file_path = handle_special_matching(source_file_path)

    if POST_INJECTOR_CONFIG["ENABLE_RC_MERGER"] and filename.endswith(".rc"):
        source_file_path = run_rc_merger(source_file_path)

    if filename.endswith(".xml") and ("/etc/permissions/" in target_file_injection_path or "/etc/sysconfig/" in target_file_injection_path):
        source_file_path = sanitize_and_merge_etc_xml(source_file_path, target_file_injection_path)

    if POST_INJECTOR_CONFIG["ENABLE_BUILD_PROPERTY_MERGER"] and filename.endswith(".prop"):
        if "build.prop" in filename:
            logging.debug(f"File {source_file_path} is a property file and will be merged with the existing file instead of "
                         f"overwriting: {target_file_injection_path} directly")
            source_file_path = run_build_property_merger(source_file_path, target_file_injection_path)

    if filename in POST_INJECTOR_CONFIG["DIRECT_INJECTION_TARGET_PATH_OVERWRITE"]:
        aosp_build_out_path = get_aosp_build_out_dir(aosp_path, aosp_version)
        target_file_injection_path = os.path.join(aosp_build_out_path,
                                                  POST_INJECTOR_CONFIG["DIRECT_INJECTION_TARGET_PATH_OVERWRITE"][filename]
                                                  )
        logging.debug(f"Direct Injection via specific target path overwrite "
                     f"for file: {filename} into {target_file_injection_path}")

    if (filename in POST_INJECTOR_CONFIG["APEX_BINARY_ISOLATED_NAMESPACE_LIST"] or source_file_path in
            POST_INJECTOR_CONFIG["APEX_BINARY_ISOLATED_NAMESPACE_LIST"]):
        logging.debug(
            f"Direct Injection via APEX symlink file: {filename} with path {source_file_path} into {target_file_injection_path}")
        is_injected = inject_apex_symlink_file(filename=filename,
                                               source_file_path=source_file_path,
                                               original_file_path=target_file_injection_path,
                                               aosp_path=aosp_path,
                                               partition_name=partition_name,
                                               lunch_target=lunch_target,
                                               aosp_version=aosp_version)
        if not is_injected:
            logging.error(f"Error injecting APEX symlink file: {source_file_path} into {target_file_injection_path}")
            return target_file_injection_path
    elif os.path.exists(target_file_injection_path):
        if os.path.islink(target_file_injection_path):
            try:
                shutil.copy2(source_file_path, target_file_injection_path, follow_symlinks=False)
                is_injected = True
                logging.debug(f"File link overwrite: {source_file_path} into {target_file_injection_path}")
            except Exception as e:
                logging.error(f"Error copying file link: {source_file_path} -> {target_file_injection_path} | {e}")
        else:
            # Fîle should not already exist, but if it does, we overwrite it, but it is not recommended.
            is_injected = True
            logging.debug(f"Direct Injection File overwrite: {source_file_path} into {target_file_injection_path}. ")
            schedule_copy(str(source_file_path), str(target_file_injection_path))
            #shutil.copy2(str(source_file_path), str(target_file_injection_path), follow_symlinks=False)
    else:
        logging.debug(f"Injecting file: {source_file_path} into {target_file_injection_path}\n")
        if not os.path.exists(source_file_path):
            logging.error(f"Injecting file: Source file does not exist anymore: {source_file_path}")
        else:
            try:
                os.makedirs(os.path.dirname(target_file_injection_path), exist_ok=True)
            except Exception as e:
                logging.error(f"Error creating directory: {target_file_injection_path} -> {e}")
            try:
                if os.path.isfile(source_file_path) and not os.path.islink(source_file_path):
                    schedule_copy(source_file_path, target_file_injection_path)
                    #shutil.copy2(source_file_path, target_file_injection_path, follow_symlinks=False)
                    is_injected = True
                elif os.path.islink(source_file_path):
                    # sudo
                    command = f'cp -a {source_file_path} {target_file_injection_path} '
                    result = subprocess.run(command, shell=True, capture_output=True, text=True)
                    if result.returncode != 0:
                        logging.error(
                            f"Inject File Error copying symlink: {source_file_path} with {target_file_injection_path} | {result.stderr}")
                    else:
                        is_injected = True
            except Exception as e:
                logging.error(f"Inject File Error copying file: {source_file_path} -> {target_file_injection_path} | {e}")

        if not set_executable_permission(target_file_injection_path):
            raise PermissionError(f"Permission denied for not existing file inject: {target_file_injection_path}")

    if ENABLE_INJECTION_PERFORMANCE_LOG:
        try:
            write_json_nd_output({
                'function': 'inject_file_into_partition',
                'source_file': source_file_path,
                'target_file': target_file_injection_path,
                'method': 'direct',
                'duration_seconds': time.time() - start_time,
                'timestamp': time.time(),
                'injected': bool(is_injected)
            }, PATH_MAPPING_EXECUTION_TIME_LOG)
        except Exception:
            logging.debug('Could not write inject_file_into_partition timing to log')

    return target_file_injection_path, is_injected



def handle_special_matching(source_file_injection_path):
    if source_file_injection_path.endswith("app_process32"):
        source_file_injection_path = source_file_injection_path.replace("app_process32", "app_process64")
        logging.debug(f"Special matching app_process32 replace with app_process64: {source_file_injection_path}")

    if source_file_injection_path.endswith("mediaserver") and "64" not in source_file_injection_path:
        if os.path.exists(source_file_injection_path.replace("mediaserver", "mediaserver64")):
            source_file_injection_path = source_file_injection_path.replace("mediaserver", "mediaserver64")
            logging.debug(f"Special matching mediaserver replace with mediaserver64: {source_file_injection_path}")
    return source_file_injection_path

def inject_apex_symlink_file(filename, source_file_path, original_file_path, aosp_path, partition_name, lunch_target, aosp_version):
    # Special case for isolated namespace binaries - Replacing Binary with symlink to apex binary
    target_path = f"/apex/com.android.fmd.{filename}.apex/bin/{filename}"
    logging.debug(f"Add new dangling symlink script: {original_file_path} -> {target_path}")
    root_path = get_path_up_to_first_term(source_file_path, partition_name)
    logging.debug(f"{source_file_path} - Root path: {root_path}")
    relative_source_path = source_file_path.replace(root_path, "")
    aosp_build_out_path = get_aosp_build_out_dir(aosp_path, aosp_version)
    abs_source_path = os.path.join(aosp_build_out_path, relative_source_path)
    inject_commands = [f"os.remove('{abs_source_path}')",f"subprocess.call(['ln', '-s', '{target_path}', '{abs_source_path}'])"]
    injection_marker = "####### FMD INJECTION MARKER #######"
    build_image_file_path = os.path.join(aosp_path, "build/make/tools/releasetools/build_image.py")
    enable_isolated_namespace = POST_INJECTOR_CONFIG["USE_ISOLATED_NAMESPACE"]
    is_injected = True
    if enable_isolated_namespace:
        if os.path.exists(build_image_file_path):
            logging.debug(f"Injecting file from Goldfish for {target_path}: {build_image_file_path}")
        try:
            with open(build_image_file_path, "r") as f:
                lines = f.readlines()

            with open(build_image_file_path, "w") as f:
                for line in lines:
                    logging.debug(f"Processing line: {line.strip()}")
                    if injection_marker in line:
                        logging.debug(f"Injection marker found in {build_image_file_path}, injecting commands.")
                        f.write(f"{injection_marker}\n")
                        for command in inject_commands:
                            logging.debug(f"Injecting command to goldfish: {command}")
                            f.write(f"    {command}\n")
                        is_injected = True
                        logging.debug(f"Injected file as simlink: {source_file_path} -> {target_path}")
                    else:
                        logging.debug(f"Write line to goldfish: {line.strip()}")
                        f.write(line)
        except Exception as e:
            logging.error(f"Error injecting file into Goldfish: {build_image_file_path} | {e}")
            is_injected = False
    return is_injected


def inject_file_into_obj(source_file_path, original_file_path, module_type, aosp_path, partition_name, lunch_target, aosp_version):
    """
    Injects a file into the AOSP source code directly without matching to existing files.
    """
    filename = os.path.basename(source_file_path)
    if POST_INJECTOR_CONFIG.get("ENABLE_RC_MERGER") and filename.endswith(".rc"):
        source_file_path = run_rc_merger(source_file_path)
    matching_intermediate_file_list = []
    try:
        org_md5 = get_md5_from_file(original_file_path)
        matching_intermediate_file_list = find_intermediate_file(aosp_path, org_md5)
        logging.debug(f"Overwriting Obj file: {source_file_path} into {original_file_path}:{org_md5}")
    except Exception as e:
        logging.error(f"Error injecting file: {source_file_path} | {e}")


    file_name = os.path.basename(original_file_path)
    start_time = time.time()
    is_injected = False
    try:
        schedule_copy(source_file_path, original_file_path)
        is_injected = True
        set_executable_permission(original_file_path)
        for file_path in matching_intermediate_file_list:
            try:
                # Skip file, if path contains specific keyword
                if any(keyword in file_path for keyword in POST_INJECTOR_CONFIG["SKIPPED_INTERMEDIATE_FILE_OVERWRITE_KEYWORD_LIST"]):
                    logging.debug(f"Skipping {file_path} because it was a keyword in SKIPPED_INTERMEDIATE_FILE_OVERWRITE_KEYWORD_LIST that matched the file_path")
                    continue
                schedule_copy(source_file_path, file_path)
                logging.debug(f"Scheduled indirect injection of .intermediate file: {file_path} with {source_file_path}")
            except Exception as e:
                logging.error(f"Error scheduling copy for intermediate file: {file_path} with {source_file_path} | {e}")
                #os.chmod(original_file_path, os.stat(original_file_path).st_mode | stat.S_IEXEC)
    except Exception as e:
        logging.error(f"Error injecting file: {source_file_path} into {original_file_path} | {e}")
        traceback.print_exc()
        is_injected = False
    finally:
        if ENABLE_INJECTION_PERFORMANCE_LOG:
            try:
                write_json_nd_output({
                    'function': 'inject_file_into_obj',
                    'source_file': source_file_path,
                    'target_file': original_file_path,
                    'method': 'indirect_obj',
                    'duration_seconds': time.time() - start_time,
                    'timestamp': time.time(),
                    'injected': bool(is_injected)
                }, PATH_MAPPING_EXECUTION_TIME_LOG)
            except Exception:
                logging.debug('Could not write inject_file_into_obj timing to log')

    return is_injected

def parse_arguments():
    """
    Parse the command line arguments.
    """
    parser = argparse.ArgumentParser(prog='aosp_post_build_injector',
                                     description="A cli tool to inject files into AOSP after the build.")
    parser.add_argument("-s",
                        "--source-path",
                        default=None,
                        required=False,
                        type=str,
                        help='Path to the source folder where the objects to inject reside.')
    parser.add_argument("-t",
                          "--target-out-path",
                        default=None,
                        required=False,
                        type=str,
                        help='Path to the AOSP target out folder.')
    parser.add_argument("-a",
                        "--aosp-root-path",
                        default=None,
                        required=False,
                        type=str,
                        help='Path to the AOSP root folder.')
    parser.add_argument("-m", "--pre_injector_config",
                        type=str,
                        default="./device_configs/development/pre_injector_config_v1.json", )
    parser.add_argument("-i", "--post_injector_config",
                        type=str,
                        default="./device_configs/development/post_injector_config_v1.json", )
    parser.add_argument("-e", "--aosp-version", type=str, default="12",)
    parser.add_argument("-u", "--fmd-username", type=str, default=None, required=False,
                        help="Username for the authentication to the fmd service.")
    parser.add_argument("-f", "--firmware-id", type=str, default=None, required=False,
                        help="ID of the firmware used in the pre-injector.")
    parser.add_argument("-c", "--use-file-config",
        action="store_true",
        help="Enable the file configuration"
    )
    parser.add_argument("-p", "--partition-list",
                        type=lambda s: [item.strip() for item in s.split(',')],  # Strip spaces too
                        default=None,
                        required=False,
                        help="Comma-separated list of partitions")
    parser.add_argument("-g", "--tag", type=str, default=None, required=False,
                        help="Tag for the uploaded artefact name.")

    args = parser.parse_args()

    return args


def main():
    logging.info("=======================AOSP POST BUILD INJECTOR=======================")
    args = parse_arguments()

    if args.use_file_config:
        base_dir = Path(os.path.dirname(os.path.abspath(__file__)))

        config_file_path = "out/post_builder_args.json"
        config_file_path_abs = base_dir / config_file_path

        if not os.path.exists(config_file_path_abs):
            logging.warning(f"Config file not found at {config_file_path_abs}. Skipping injection.")
            # We use exit 0 because we want the build process to skip in case the file does not exist yet
            exit(0)

        with open(config_file_path_abs, "r", encoding="utf-8") as f:
            post_builder_args_dict = json.load(f)

        aosp_version = post_builder_args_dict.get("aosp_version", None)
        source_folder_path = post_builder_args_dict.get("source_folder_path", None)
        target_out_path = post_builder_args_dict.get("target_out_path", None)
        aosp_path = post_builder_args_dict.get("aosp_path", None)
        lunch_target = post_builder_args_dict.get("lunch_target", None)
        firmware_id = post_builder_args_dict.get("firmware_id", None)
        pre_injector_package_list = post_builder_args_dict.get("pre_injector_package_list", None)
        tag = post_builder_args_dict.get("tag", None)
        pre_rel = post_builder_args_dict.get("pre_injector_config_path")
        post_rel = post_builder_args_dict.get("post_injector_config_path")

        pre_injector_config_path = str((base_dir / pre_rel).resolve()) if pre_rel else None
        post_injector_config_path = str((base_dir / post_rel).resolve()) if post_rel else None

        logging.info(
            f"pre_injector_config_path: {pre_injector_config_path}, post_injector_config_path: {post_injector_config_path}")
    else:
        aosp_version = args.aosp_version
        source_folder_path = args.source_path
        target_out_path = args.target_out_path
        aosp_path = args.aosp_root_path
        if aosp_version in ["12", "12.1"]:
            lunch_target = "sdk_phone64_arm64-userdebug"
        elif aosp_version == "13":
            lunch_target = "sdk_phone64_arm64-userdebug"
        elif aosp_version == "14":
            lunch_target = "sdk_phone64_arm64-ap2a-userdebug"
        elif aosp_version in ["15", "16"]:
            lunch_target = "sdk_phone64_arm64-trunk_staging-userdebug"
        else:
            raise RuntimeError("Please provide a valid lunch target argument.")
        firmware_id = args.firmware_id
        pre_injector_package_list = []
        pre_injector_config_path = args.pre_injector_config
        post_injector_config_path = args.post_injector_config
        tag = args.tag

    partition_list = args.partition_list
    if not partition_list:
        partition_list = []

    if aosp_version not in ["12", "12.1", "13", "14", "15", "16"]:
        raise RuntimeError("Please provide a valid AOSP version argument (12, 13, 14, 15, 16).")
    if not firmware_id:
        raise RuntimeError("Please provide a firmware ID argument.")
    if not aosp_path:
        raise RuntimeError("Please provide an AOSP path argument.")

    fmd_password = os.getenv('FMD_PASSWORD')
    fmd_username = os.getenv("FMD_USERNAME") or args.fmd_username
    if not source_folder_path.endswith("/"):
        source_folder_path += "/"
    if not target_out_path.endswith("/"):
        target_out_path += "/"
    if aosp_path and not aosp_path.endswith("/"):
        aosp_path += "/"



    logging.info(f"Source folder path: {source_folder_path}")
    logging.info(f"Target out path: {target_out_path}")
    logging.info(f"AOSP root path: {aosp_path}")
    logging.info(f"Lunch target: {lunch_target}")
    logging.info(f"Pre Injector Config: {pre_injector_config_path}")
    logging.info(f"Post Injector Config: {post_injector_config_path}")
    pre_injector_config, post_injector_config = load_configs(pre_injector_config_path, post_injector_config_path)

    if fmd_username and fmd_password:
        try:
            fmd_url = post_injector_config["FMD_URL"]
            graphql_url = post_injector_config["GRAPHQL_API_URL"]
            csrf_cookie = get_csrf_token(fmd_url)
            fmd_cookies = authenticate_fmd(graphql_url, fmd_username, fmd_password, csrf_cookie)
        except Exception as e:
            logging.error(f"Error occurred while authenticating with FMD: {e}")
            fmd_cookies = None
    else:
        logging.warning("No FMD username or FMD password provided. APK signing might fail")
        fmd_cookies = None

    start_post_build_injector(aosp_path=aosp_path,
                              source_folder_path=source_folder_path,
                              target_out_path=target_out_path,
                              lunch_target=lunch_target,
                              firmware_id=firmware_id,
                              pre_injector_package_list=pre_injector_package_list,
                              pre_injector_config_path=pre_injector_config_path,
                              post_injector_config_path=post_injector_config_path,
                              cookies=fmd_cookies,
                              aosp_version=aosp_version,
                              partition_list=partition_list,
                              tag=tag)




    logging.info("=======================AOSP POST BUILD INJECTOR EXIT=======================")


if __name__ == "__main__":
    main()
