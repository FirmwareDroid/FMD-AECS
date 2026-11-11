import glob
import json
import logging
import os
import re
import shutil
import struct
import tempfile
import traceback
import xml.etree.ElementTree as ET
import zipfile

from ConfigManager import ConfigManager
from common import get_md5_from_file
from fmd_backend_requests import fetch_app_manifest
from shell_command import execute_command
from config_post_injector import *

POST_INJECTOR_CONFIG = {}

APKSIGNER_BINARY_PATH_LIST = ["out/host/linux-x86/bin/apksigner",
                              "prebuilts/sdk/tools/linux/bin/apksigner",
                              "apksigner",
                              "out/host/common/obj/JAVA_LIBRARIES/apksigner_intermediates/classes.jar"
                              ]
APKSIGNER_JAVALIB_PATH = "prebuilts/sdk/tools/linux/lib/apksigner.jar"
ZIPALIGN_BINARY_PATH_LIST = ["prebuilts/sdk/tools/linux/bin/zipalign", "out/host/linux-x86/bin/zipalign"]
AAPT2_BINARY_PATH_LIST = [
    "out/host/linux-x86/bin/aapt2",
    "prebuilts/build-tools/linux-x86/bin/aapt2",
    "prebuilts/sdk/tools/linux/bin/aapt2",
]


def get_apksigner_binary_command(aosp_path):
    candidate_list = []
    for binary_path in APKSIGNER_BINARY_PATH_LIST:
        if "/" in binary_path:
            if aosp_path:
                candidate = os.path.join(aosp_path, binary_path)
                if os.path.exists(candidate):
                    candidate_list.append(candidate)
        else:
            candidate_list.append(binary_path)

    return candidate_list


def get_zipalign_binary_command(aosp_path):
    if aosp_path:
        for binary_path in ZIPALIGN_BINARY_PATH_LIST:
            candidate = os.path.join(aosp_path, binary_path)
            if os.path.exists(candidate):
                return candidate
    return shutil.which("zipalign") or "zipalign"


def get_aapt2_binary_command(aosp_path=None):
    if aosp_path:
        for binary_path in AAPT2_BINARY_PATH_LIST:
            candidate = os.path.join(aosp_path, binary_path)
            if os.path.exists(candidate):
                return candidate
    return shutil.which("aapt2")


def _resolve_vdex_and_cdex_binaries(aosp_path=None):
    vdex_bin = shutil.which("vdexExtractor")
    if not vdex_bin and aosp_path:
        for rel in ("out/host/linux-x86/bin/vdexExtractor", "prebuilts/misc/common/vdexExtractor/vdexExtractor"):
            cand = os.path.join(aosp_path, rel)
            if os.path.exists(cand):
                vdex_bin = cand
                break

    cdex_bin = shutil.which("compact_dex_converter")
    if not cdex_bin and aosp_path:
        for rel in ("out/host/linux-x86/bin/compact_dex_converter",):
            cand = os.path.join(aosp_path, rel)
            if os.path.exists(cand):
                cdex_bin = cand
                break
    return vdex_bin, cdex_bin


def _find_companion_vdex_files(archive_path, source_root_path=None):
    """
    Search for companion .vdex/.odex files for an APK or JAR across standard oat/
    and boot-*.vdex locations.
    """
    archive_dir = os.path.dirname(archive_path)
    stem = os.path.splitext(os.path.basename(archive_path))[0]
    is_jar = archive_path.lower().endswith(".jar")

    vdex_candidates = []
    odex_candidates = []

    def _add_glob(pattern, target_list):
        for match in sorted(glob.glob(pattern)):
            if match not in target_list and os.path.isfile(match):
                target_list.append(match)

    # 1. Local oat/<arch>/<stem>.vdex and .odex
    _add_glob(os.path.join(archive_dir, "oat", "*", f"{stem}.vdex"), vdex_candidates)
    _add_glob(os.path.join(archive_dir, "oat", "*", f"{stem}.odex"), odex_candidates)

    if is_jar:
        # 2. Bootclasspath vdex in <framework_dir>/<arch>/boot-<stem>.vdex or <framework_dir>/boot-<stem>.vdex
        _add_glob(os.path.join(archive_dir, "*", f"boot-{stem}.vdex"), vdex_candidates)
        _add_glob(os.path.join(archive_dir, f"boot-{stem}.vdex"), vdex_candidates)
        _add_glob(os.path.join(archive_dir, "oat", "*", f"boot-{stem}.vdex"), vdex_candidates)
        if stem == "framework":
            _add_glob(os.path.join(archive_dir, "*", "boot.vdex"), vdex_candidates)
            _add_glob(os.path.join(archive_dir, "boot.vdex"), vdex_candidates)

        # 3. Search across source_root_path/system/framework if boot image resides on system partition
        if source_root_path and os.path.isdir(source_root_path):
            for sys_fw in (
                os.path.join(source_root_path, "system", "framework"),
                os.path.join(source_root_path, "system", "system", "framework"),
            ):
                if os.path.isdir(sys_fw):
                    _add_glob(os.path.join(sys_fw, "*", f"boot-{stem}.vdex"), vdex_candidates)
                    _add_glob(os.path.join(sys_fw, "oat", "*", f"{stem}.vdex"), vdex_candidates)
                    _add_glob(os.path.join(sys_fw, f"boot-{stem}.vdex"), vdex_candidates)
                    if stem == "framework":
                        _add_glob(os.path.join(sys_fw, "*", "boot.vdex"), vdex_candidates)

    return vdex_candidates, odex_candidates


def _convert_cdex_if_needed(dex_file_path, cdex_bin):
    """
    If dex_file_path is a CompactDex file (starts with b'cdex' or ends with .cdex),
    converts it to a standard DEX file using compact_dex_converter.
    Returns path to the standard DEX file, or None if conversion failed.
    """
    if not os.path.exists(dex_file_path):
        return None
    try:
        with open(dex_file_path, "rb") as f:
            magic = f.read(4)
    except Exception:
        return None

    is_cdex = magic == b"cdex" or dex_file_path.endswith(".cdex")
    if not is_cdex:
        return dex_file_path

    if not cdex_bin:
        logging.warning(f"CompactDex file detected ({dex_file_path}) but compact_dex_converter is not available.")
        return None

    out_dir = os.path.dirname(dex_file_path)
    cmd = [cdex_bin, "-w", out_dir, dex_file_path]
    success, _ = execute_command(cmd)
    # compact_dex_converter typically writes <dex_file_path>.new
    converted_candidate = f"{dex_file_path}.new"
    if success and os.path.exists(converted_candidate):
        std_dex_path = re.sub(r"\.cdex$", ".dex", dex_file_path)
        shutil.move(converted_candidate, std_dex_path)
        return std_dex_path

    # Check if converted in-place
    try:
        with open(dex_file_path, "rb") as f:
            if f.read(4) == b"dex\n":
                return dex_file_path
    except Exception:
        pass
    return None


def ensure_archive_has_dex(archive_path, aosp_path=None, source_root_path=None, require_dex=False):
    """
    Checks whether an APK or JAR archive contains classes*.dex.
    If classes.dex is missing, searches for companion .vdex files (in oat/<arch>/ and boot-*.vdex),
    extracts DEX/CompactDex via vdexExtractor (+ compact_dex_converter), and packs them into the archive.
    Returns (bool, str | None): (True, None) if valid, or (False, reason) if stripped and unrecoverable.
    """
    if not os.path.exists(archive_path):
        return False, f"Archive file does not exist: {archive_path}"
    try:
        with zipfile.ZipFile(archive_path, 'r') as zf:
            names = zf.namelist()
            if any(name.endswith(".dex") for name in names):
                return True, None
    except Exception as e:
        return False, f"Invalid or corrupt zip container {archive_path}: {e}"

    is_jar = archive_path.lower().endswith(".jar")
    stem = os.path.splitext(os.path.basename(archive_path))[0]
    vdex_candidates, odex_candidates = _find_companion_vdex_files(archive_path, source_root_path=source_root_path)

    if not vdex_candidates and not odex_candidates:
        if is_jar or require_dex:
            return False, f"Stripped JAR/archive missing classes.dex and no companion .vdex found: {archive_path}"
        # For APKs without an oat/ folder or .vdex, check if manifest declares hasCode=false or resource-only
        manifest_info = inspect_apk_manifest_locally(archive_path, aosp_path)
        if manifest_info.get("has_code") is False:
            return True, None
        oat_dir = os.path.join(os.path.dirname(archive_path), "oat")
        if not os.path.isdir(oat_dir):
            return True, None
        return False, f"Stripped APK missing classes.dex: {archive_path}"

    vdex_bin, cdex_bin = _resolve_vdex_and_cdex_binaries(aosp_path)
    if vdex_bin and vdex_candidates:
        for vdex_file in vdex_candidates:
            try:
                with tempfile.TemporaryDirectory() as tmp_dir:
                    cmd = [vdex_bin, "-i", vdex_file, "-o", tmp_dir, "-f"]
                    execute_command(cmd)
                    raw_extracted = []
                    for pat in (
                        f"{stem}_classes*.dex",
                        f"{stem}_classes*.cdex",
                        f"boot-{stem}_classes*.dex",
                        f"boot-{stem}_classes*.cdex",
                        "*.dex",
                        "*.cdex",
                    ):
                        matched = sorted(glob.glob(os.path.join(tmp_dir, pat)))
                        if matched:
                            raw_extracted = matched
                            break

                    valid_dex_files = []
                    for raw_dex in raw_extracted:
                        std_dex = _convert_cdex_if_needed(raw_dex, cdex_bin)
                        if std_dex and os.path.exists(std_dex):
                            valid_dex_files.append(std_dex)

                    if valid_dex_files:
                        with zipfile.ZipFile(archive_path, 'a', compression=zipfile.ZIP_STORED) as zf:
                            for idx, dex_path in enumerate(valid_dex_files):
                                arcname = "classes.dex" if idx == 0 else f"classes{idx + 1}.dex"
                                zf.write(dex_path, arcname=arcname)
                        logging.info(
                            f"Recovered {len(valid_dex_files)} DEX file(s) from {vdex_file} into {archive_path}"
                        )
                        return True, None
            except Exception as e:
                logging.warning(f"Failed to recover DEX from {vdex_file} for {archive_path}: {e}")

    return False, (
        f"Stripped archive missing classes.dex (companion vdex/odex files found: "
        f"{vdex_candidates or odex_candidates}, but DEX recovery failed) for {archive_path}"
    )


def ensure_apk_has_dex(apk_file_path, aosp_path, source_root_path=None):
    """Backward-compatible wrapper around ensure_archive_has_dex for APK files."""
    return ensure_archive_has_dex(apk_file_path, aosp_path=aosp_path, source_root_path=source_root_path, require_dex=False)


def _resolve_baksmali_and_smali_cmds(aosp_path=None):
    """
    Locate baksmali and smali executables or JARs (from PATH, ~/acvtool/config.json,
    smiler package, or AOSP host/prebuilts).
    Returns (baksmali_cmd_prefix_list, smali_cmd_prefix_list) or (None, None).
    """
    baksmali_bin = shutil.which("baksmali")
    smali_bin = shutil.which("smali")
    if baksmali_bin and smali_bin:
        return [baksmali_bin], [smali_bin]

    candidate_pairs = []
    # Check ~/acvtool/config.json
    acv_cfg_path = os.path.expanduser("~/acvtool/config.json")
    if os.path.exists(acv_cfg_path):
        try:
            with open(acv_cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            b_jar = cfg.get("BAKSMALI") or cfg.get("baksmali")
            s_jar = cfg.get("SMALI") or cfg.get("smali")
            if b_jar and s_jar:
                candidate_pairs.append((os.path.expanduser(b_jar), os.path.expanduser(s_jar)))
        except Exception:
            pass

    if aosp_path:
        candidate_pairs.extend([
            (
                os.path.join(aosp_path, "out/host/linux-x86/framework/baksmali.jar"),
                os.path.join(aosp_path, "out/host/linux-x86/framework/smali.jar"),
            ),
            (
                os.path.join(aosp_path, "prebuilts/misc/common/smali/baksmali.jar"),
                os.path.join(aosp_path, "prebuilts/misc/common/smali/smali.jar"),
            ),
        ])

    for b_jar, s_jar in candidate_pairs:
        if os.path.exists(b_jar) and os.path.exists(s_jar):
            return ["java", "-jar", b_jar], ["java", "-jar", s_jar]

    return None, None


def _patch_system_service_manager_smali_text(smali_text):
    """
    Patches SystemServiceManager.smali so that startService(SystemService)V and
    startBootPhase(...)V catch all Throwables from failing OEM SystemServices,
    log them via Slog.e, remove the broken service from mServices (in startService),
    and return cleanly instead of crashing SystemServer.
    Returns (patched_smali_text, num_patches_applied).
    """
    method_pattern = re.compile(
        r"(^\.method\s+[^\n]*\b(startService\(Lcom/android/server/SystemService;\)V|startBootPhase\([^\n]*\)V)\b.*?^\.end method)",
        re.DOTALL | re.MULTILINE,
    )
    patches_applied = 0

    def _patch_method(match):
        nonlocal patches_applied
        block = match.group(1)
        sig = match.group(2)
        is_start_service = sig.startswith("startService(")

        # Widen RuntimeException / Exception catches inside this void method to Throwable
        widened = re.sub(
            r"\.catch\s+Ljava/lang/(?:RuntimeException|Exception);",
            ".catch Ljava/lang/Throwable;",
            block,
        )

        # Replace `throw vX` in the catch handler that builds the RuntimeException
        def _replace_throw(throw_match):
            nonlocal patches_applied
            reg = throw_match.group(1)
            patches_applied += 1
            lines = [
                f'    const-string v0, "FMD_SystemServiceManager"',
                f'    const-string v1, "Suppressed vendor SystemService crash in {sig}"',
                f'    invoke-static {{v0, v1, {reg}}}, Landroid/util/Slog;->e(Ljava/lang/String;Ljava/lang/String;Ljava/lang/Throwable;)I',
            ]
            if is_start_service:
                lines.extend([
                    '    iget-object v0, p0, Lcom/android/server/SystemServiceManager;->mServices:Ljava/util/ArrayList;',
                    '    invoke-virtual {v0, p1}, Ljava/util/ArrayList;->remove(Ljava/lang/Object;)Z',
                ])
            lines.append("    return-void")
            return "\n".join(lines)

        patched_block = re.sub(r"^\s*throw\s+(v\d+)\s*$", _replace_throw, widened, flags=re.MULTILINE)
        return patched_block

    new_text = method_pattern.sub(_patch_method, smali_text)
    return new_text, patches_applied


def patch_services_jar_fault_isolation(services_jar_path, aosp_path=None):
    """
    Disassembles the DEX containing com/android/server/SystemServiceManager in services.jar,
    applies fault-isolation try/catch patching so failing OEM SystemServices do not abort
    SystemServer, and reassembles the DEX back into services.jar.
    Returns (bool, str): (patched_ok, status_message).
    """
    if not os.path.exists(services_jar_path):
        return False, f"services.jar not found: {services_jar_path}"

    baksmali_cmd, smali_cmd = _resolve_baksmali_and_smali_cmds(aosp_path)
    if not baksmali_cmd or not smali_cmd:
        msg = "baksmali/smali not found on host; skipping SystemServiceManager bytecode patch"
        logging.debug(msg)
        return False, msg

    target_class_desc = b"Lcom/android/server/SystemServiceManager;"
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            target_dex_name = None
            with zipfile.ZipFile(services_jar_path, "r") as zf:
                for name in zf.namelist():
                    if name.endswith(".dex"):
                        data = zf.read(name)
                        if target_class_desc in data:
                            target_dex_name = name
                            dex_path = os.path.join(tmp_dir, name)
                            with open(dex_path, "wb") as out_f:
                                out_f.write(data)
                            break

            if not target_dex_name:
                return False, "SystemServiceManager class not found in services.jar DEX files"

            smali_out_dir = os.path.join(tmp_dir, "smali_out")
            dis_cmd = [*baksmali_cmd, "d", dex_path, "-o", smali_out_dir]
            ok, log_msg = execute_command(dis_cmd)
            target_smali = os.path.join(smali_out_dir, "com", "android", "server", "SystemServiceManager.smali")
            if not ok or not os.path.exists(target_smali):
                return False, f"baksmali failed on {services_jar_path}: {log_msg}"

            with open(target_smali, "r", encoding="utf-8") as sf:
                original_smali = sf.read()

            patched_smali, count = _patch_system_service_manager_smali_text(original_smali)
            if count == 0:
                return False, "No throw sites matched in SystemServiceManager.smali"

            with open(target_smali, "w", encoding="utf-8") as sf:
                sf.write(patched_smali)

            rebuilt_dex = os.path.join(tmp_dir, f"rebuilt_{target_dex_name}")
            asm_cmd = [*smali_cmd, "a", smali_out_dir, "-o", rebuilt_dex]
            ok, log_msg = execute_command(asm_cmd)
            if not ok or not os.path.exists(rebuilt_dex):
                return False, f"smali reassembly failed on {services_jar_path}: {log_msg}"

            # Repackage services.jar replacing target_dex_name
            rebuilt_jar = os.path.join(tmp_dir, "services_patched.jar")
            with zipfile.ZipFile(services_jar_path, "r") as zin, zipfile.ZipFile(rebuilt_jar, "w") as zout:
                for item in zin.infolist():
                    if item.filename == target_dex_name:
                        zout.write(rebuilt_dex, arcname=target_dex_name, compress_type=zipfile.ZIP_STORED)
                    else:
                        zout.writestr(item, zin.read(item.filename))

            shutil.move(rebuilt_jar, services_jar_path)
            logging.info(
                f"Patched SystemServiceManager fault isolation ({count} site(s)) in {services_jar_path}"
            )
            return True, f"Patched {count} site(s) in {target_dex_name}"
    except Exception as e:
        logging.warning(f"Exception while patching services.jar ({services_jar_path}): {e}")
        return False, str(e)


def handle_jar_module(jar_file_path, aosp_path=None, source_root_path=None):
    """
    Validates and prepares a vendor .jar file (JAVA_LIBRARIES) prior to injection:
    1. Ensures classes*.dex are present inside the .jar (de-odexing from oat/ or boot-*.vdex
       and converting CompactDex if needed).
    2. If the .jar is services.jar, applies SystemServiceManager fault-isolation patching.
    Returns None on success, or an error string if the .jar is stripped and unrecoverable.
    """
    has_dex, dex_err = ensure_archive_has_dex(
        jar_file_path,
        aosp_path=aosp_path,
        source_root_path=source_root_path,
        require_dex=True,
    )
    if not has_dex:
        return f"Skipping stripped/unrecoverable JAR file: {jar_file_path} | {dex_err}"

    if os.path.basename(jar_file_path) == "services.jar":
        patch_services_jar_fault_isolation(jar_file_path, aosp_path=aosp_path)

    return None


def _parse_axml_manifest_bytes(data):
    """
    Pure-Python parser for Android binary XML (AXML) or plain-text XML AndroidManifest.xml.
    Extracts package_name, shared_user_id, uses_permissions, provider_authorities,
    target_sdk_version, has_code, is_rro_overlay, and overlay_target_package.
    """
    info = {
        "package_name": None,
        "shared_user_id": None,
        "uses_permissions": [],
        "provider_authorities": [],
        "target_sdk_version": None,
        "has_code": True,
        "is_rro_overlay": False,
        "overlay_target_package": None,
    }
    if not data:
        return info

    # 1. Check if plain-text XML
    stripped = data.lstrip()
    if stripped.startswith(b"<?xml") or stripped.startswith(b"<manifest"):
        try:
            xml_str = data.decode("utf-8", errors="ignore")
            if "xmlns:android=" not in xml_str and "<manifest" in xml_str:
                xml_str = xml_str.replace(
                    "<manifest",
                    '<manifest xmlns:android="http://schemas.android.com/apk/res/android"',
                    1,
                )
            root = ET.fromstring(xml_str)
            info["package_name"] = root.attrib.get("package")
            for k, v in root.attrib.items():
                if k.endswith("sharedUserId"):
                    info["shared_user_id"] = v
            for elem in root.iter():
                tag = elem.tag.split("}")[-1]
                if tag in ("uses-permission", "uses-permission-sdk-23"):
                    for k, v in elem.attrib.items():
                        if k.endswith("name") and v and v not in info["uses_permissions"]:
                            info["uses_permissions"].append(v)
                elif tag == "provider":
                    for k, v in elem.attrib.items():
                        if k.endswith("authorities") and v:
                            for auth in v.split(";"):
                                auth = auth.strip()
                                if auth and auth not in info["provider_authorities"]:
                                    info["provider_authorities"].append(auth)
                elif tag == "uses-sdk":
                    for k, v in elem.attrib.items():
                        if k.endswith("targetSdkVersion"):
                            try:
                                info["target_sdk_version"] = int(v)
                            except ValueError:
                                pass
                elif tag == "application":
                    for k, v in elem.attrib.items():
                        if k.endswith("hasCode"):
                            info["has_code"] = str(v).lower() not in ("false", "0")
                elif tag == "overlay":
                    info["is_rro_overlay"] = True
                    for k, v in elem.attrib.items():
                        if k.endswith("targetPackage") and v:
                            info["overlay_target_package"] = v
            return info
        except Exception:
            pass

    # 2. Parse binary AXML (RES_XML_TYPE = 0x0003)
    try:
        if len(data) < 8:
            return info
        xml_type, header_size, total_size = struct.unpack_from("<HHI", data, 0)
        if xml_type != 0x0003:
            return info

        offset = header_size
        strings = []
        while offset + 8 <= len(data):
            chunk_type, chunk_header_size, chunk_size = struct.unpack_from("<HHI", data, offset)
            if chunk_size < 8 or offset + chunk_size > len(data):
                break

            if chunk_type == 0x0001:  # RES_STRING_POOL_TYPE
                str_count, _style_count, flags, strings_start, _styles_start = struct.unpack_from(
                    "<IIIII", data, offset + 8
                )
                is_utf8 = bool(flags & (1 << 8))
                offsets_base = offset + 28
                pool_base = offset + strings_start
                for i in range(str_count):
                    if offsets_base + (i + 1) * 4 > offset + chunk_size:
                        break
                    str_off = struct.unpack_from("<I", data, offsets_base + i * 4)[0]
                    pos = pool_base + str_off
                    if pos >= len(data):
                        strings.append("")
                        continue
                    if is_utf8:
                        # UTF-8 length varint (1 or 2 bytes) twice
                        u16_len = data[pos]
                        pos += 2 if (u16_len & 0x80) else 1
                        u8_len = data[pos]
                        if u8_len & 0x80:
                            u8_len = ((u8_len & 0x7F) << 8) | data[pos + 1]
                            pos += 2
                        else:
                            pos += 1
                        s = data[pos:pos + u8_len].decode("utf-8", errors="replace")
                    else:
                        u16_len = struct.unpack_from("<H", data, pos)[0]
                        if u16_len & 0x8000:
                            u16_len = ((u16_len & 0x7FFF) << 16) | struct.unpack_from("<H", data, pos + 2)[0]
                            pos += 4
                        else:
                            pos += 2
                        s = data[pos:pos + u16_len * 2].decode("utf-16le", errors="replace")
                    strings.append(s)

            elif chunk_type == 0x0102:  # RES_XML_START_ELEMENT_TYPE
                if offset + 36 <= len(data):
                    _ns_idx, name_idx, attr_start, attr_size, attr_count = struct.unpack_from(
                        "<iiHHH", data, offset + 16
                    )
                    elem_name = strings[name_idx] if 0 <= name_idx < len(strings) else ""
                    attr_base = offset + 16 + attr_start
                    attrs = {}
                    for i in range(attr_count):
                        a_off = attr_base + i * attr_size
                        if a_off + 20 > offset + chunk_size:
                            break
                        _a_ns, a_name_idx, a_raw_idx, _t_size, _t_res0, t_type, t_data = struct.unpack_from(
                            "<iiiHBBI", data, a_off
                        )
                        a_name = strings[a_name_idx] if 0 <= a_name_idx < len(strings) else ""
                        if 0 <= a_raw_idx < len(strings):
                            a_val = strings[a_raw_idx]
                        elif t_type == 0x03 and 0 <= t_data < len(strings):
                            a_val = strings[t_data]
                        elif t_type == 0x12:  # TYPE_INT_BOOLEAN
                            a_val = (t_data != 0)
                        elif t_type in (0x10, 0x11):  # TYPE_INT_DEC / HEX
                            a_val = int(t_data)
                        else:
                            a_val = t_data
                        attrs[a_name] = a_val

                    if elem_name == "manifest":
                        if isinstance(attrs.get("package"), str):
                            info["package_name"] = attrs["package"]
                        if isinstance(attrs.get("sharedUserId"), str):
                            info["shared_user_id"] = attrs["sharedUserId"]
                    elif elem_name in ("uses-permission", "uses-permission-sdk-23"):
                        perm = attrs.get("name")
                        if isinstance(perm, str) and perm and perm not in info["uses_permissions"]:
                            info["uses_permissions"].append(perm)
                    elif elem_name == "provider":
                        auths = attrs.get("authorities")
                        if isinstance(auths, str) and auths:
                            for auth in auths.split(";"):
                                auth = auth.strip()
                                if auth and auth not in info["provider_authorities"]:
                                    info["provider_authorities"].append(auth)
                    elif elem_name == "uses-sdk":
                        tsdk = attrs.get("targetSdkVersion")
                        if isinstance(tsdk, int):
                            info["target_sdk_version"] = tsdk
                        elif isinstance(tsdk, str) and tsdk.isdigit():
                            info["target_sdk_version"] = int(tsdk)
                    elif elem_name == "application" and "hasCode" in attrs:
                        hc = attrs["hasCode"]
                        info["has_code"] = bool(hc) if isinstance(hc, bool) else str(hc).lower() not in ("false", "0")
                    elif elem_name == "overlay":
                        info["is_rro_overlay"] = True
                        if isinstance(attrs.get("targetPackage"), str) and attrs["targetPackage"]:
                            info["overlay_target_package"] = attrs["targetPackage"]

            offset += chunk_size
    except Exception as e:
        logging.debug(f"AXML parser fallback encountered error: {e}")

    return info


def inspect_apk_manifest_locally(apk_file_path, aosp_path=None):
    """
    Extracts manifest metadata (package_name, shared_user_id, uses_permissions,
    provider_authorities, target_sdk_version, has_code, is_rro_overlay, overlay_target_package)
    directly from an APK file without requiring remote GraphQL calls.
    Uses built-in AXML/XML parser first, falling back to aapt2 dump xmltree when needed.
    """
    default_info = {
        "package_name": None,
        "shared_user_id": None,
        "uses_permissions": [],
        "provider_authorities": [],
        "target_sdk_version": None,
        "has_code": True,
        "is_rro_overlay": False,
        "overlay_target_package": None,
    }
    if not apk_file_path or not os.path.exists(apk_file_path):
        return default_info

    # 1. Try built-in AXML/XML parser on AndroidManifest.xml inside the APK first (fast, zero subprocess overhead)
    try:
        with zipfile.ZipFile(apk_file_path, "r") as zf:
            if "AndroidManifest.xml" in zf.namelist():
                raw_manifest = zf.read("AndroidManifest.xml")
                parsed = _parse_axml_manifest_bytes(raw_manifest)
                if parsed.get("package_name"):
                    return parsed
    except Exception as e:
        logging.debug(f"Zip/AXML inspection failed for {apk_file_path}: {e}")

    # 2. Fallback to aapt2 dump xmltree if available
    aapt2_bin = get_aapt2_binary_command(aosp_path)
    if aapt2_bin:
        cmd = [aapt2_bin, "dump", "xmltree", apk_file_path, "--file", "AndroidManifest.xml"]
        ok, out = execute_command(cmd)
        if ok and out:
            info = dict(default_info)
            info["uses_permissions"] = []
            info["provider_authorities"] = []
            current_elem = None
            for line in out.splitlines():
                stripped = line.strip()
                elem_m = re.match(r"E:\s+(\S+)", stripped)
                if elem_m:
                    current_elem = elem_m.group(1)
                    if current_elem == "overlay":
                        info["is_rro_overlay"] = True
                    continue
                if current_elem == "manifest":
                    pkg_m = re.search(r'A:\s+package="([^"]+)"', stripped)
                    if pkg_m:
                        info["package_name"] = pkg_m.group(1)
                    suid_m = re.search(r'A:\s+[\w:]*sharedUserId(?:\([^)]*\))?="([^"]+)"', stripped)
                    if suid_m:
                        info["shared_user_id"] = suid_m.group(1)
                elif current_elem in ("uses-permission", "uses-permission-sdk-23"):
                    perm_m = re.search(r'A:\s+[\w:]*name(?:\([^)]*\))?="([^"]+)"', stripped)
                    if perm_m and perm_m.group(1) not in info["uses_permissions"]:
                        info["uses_permissions"].append(perm_m.group(1))
                elif current_elem == "provider":
                    auth_m = re.search(r'A:\s+[\w:]*authorities(?:\([^)]*\))?="([^"]+)"', stripped)
                    if auth_m:
                        for auth in auth_m.group(1).split(";"):
                            auth = auth.strip()
                            if auth and auth not in info["provider_authorities"]:
                                info["provider_authorities"].append(auth)
                elif current_elem == "overlay":
                    tpkg_m = re.search(r'A:\s+[\w:]*targetPackage(?:\([^)]*\))?="([^"]+)"', stripped)
                    if tpkg_m:
                        info["overlay_target_package"] = tpkg_m.group(1)
            if info["package_name"]:
                return info

    return default_info


def handle_apk_signing(file_path, aosp_path, firmware_id, cookies):
    global POST_INJECTOR_CONFIG
    try:
        cfg = ConfigManager.get_config("POST_INJECTOR_CONFIG")
        if cfg:
            POST_INJECTOR_CONFIG = cfg
    except Exception:
        pass
    error_message = None
    output = None
    is_success = False

    has_dex, dex_err = ensure_apk_has_dex(file_path, aosp_path)
    if not has_dex:
        return False, output, (dex_err, file_path, None, None)

    signing_key = get_signing_key_from_module(file_path, firmware_id, cookies, aosp_path=aosp_path)

    if not signing_key:
        error_message = f"Signing key name not found for {file_path}"
    signing_key_path = get_signing_key_path(aosp_path, signing_key)

    if not os.path.exists(signing_key_path):
        # Fallback to platform.p12 if a specialized key (e.g. radio/oem) is absent in AOSP security dir
        fallback_platform = get_signing_key_path(aosp_path, "platform")
        if signing_key != "platform" and os.path.exists(fallback_platform):
            logging.warning(
                f"Signing key '{signing_key}' not found at {signing_key_path}; falling back to 'platform'"
            )
            signing_key = "platform"
            signing_key_path = fallback_platform
        else:
            error_message = f"Signing key not found at {signing_key_path}"

    if not error_message:
        # Always 4-byte align APK (critical for Android 11+ targetSdk>=30 uncompressed resources.arsc & .so)
        align_apk_file(file_path, aosp_path)

        is_success, log_message = sign_apk_file(file_path, signing_key_path, aosp_path)
        if not is_success:
            error_message = (f"Error signing APK file with apksigner: "
                             f"Key:{signing_key}|"
                             f"Key Path: {signing_key_path}|"
                             f"Message:{log_message}")
        else:
            logging.info(f"APK file signed: {file_path} with key: {signing_key}")
            # Remove stale v4 signature (.idsig) and stale precompiled oat/ artifacts after re-signing
            idsig_path = f"{file_path}.idsig"
            if os.path.exists(idsig_path):
                try:
                    os.remove(idsig_path)
                except Exception:
                    pass
            stale_oat_dir = os.path.join(os.path.dirname(file_path), "oat")
            if os.path.isdir(stale_oat_dir):
                try:
                    shutil.rmtree(stale_oat_dir)
                except Exception:
                    pass

    return is_success, output, (error_message, file_path, signing_key, signing_key_path)


def get_shared_user_from_manifest(firmware_id, android_apk_file_path, cookies, aosp_path=None):
    # 1. Inspect manifest locally first (no network dependency required)
    local_info = inspect_apk_manifest_locally(android_apk_file_path, aosp_path=aosp_path)
    if local_info.get("shared_user_id"):
        shared_user_id = local_info["shared_user_id"]
        logging.info(f"Shared User ID (local manifest) for {android_apk_file_path}: {shared_user_id}")
        return shared_user_id

    # 2. Fallback to remote GraphQL API if cookies and URL are configured
    if not cookies or not isinstance(POST_INJECTOR_CONFIG, dict) or not POST_INJECTOR_CONFIG.get("GRAPHQL_API_URL"):
        return None

    logging.info(f"Getting shared user from FMD GraphQL manifest: {android_apk_file_path}")
    filename = os.path.basename(android_apk_file_path)
    graphql_url = POST_INJECTOR_CONFIG["GRAPHQL_API_URL"]
    manifest = fetch_app_manifest(graphql_url, cookies, firmware_id, filename)
    if manifest and '@ns0:sharedUserId' in manifest:
        shared_user_id = manifest['@ns0:sharedUserId']
        logging.info(f"Shared User ID from FMD API for {android_apk_file_path}: {shared_user_id}")
        return shared_user_id
    return None


def _resolve_signing_key_for_shared_uid(shared_user_id):
    if not shared_user_id:
        return None
    mapping = dict(DEFAULT_SHARED_USER_ID_MAPPING_DICT)
    if isinstance(POST_INJECTOR_CONFIG, dict):
        for k, v in POST_INJECTOR_CONFIG.get("SHARED_USER_ID_MAPPING_DICT", {}).items():
            existing = list(mapping.get(k, []))
            if isinstance(v, list):
                for item in v:
                    if item not in existing:
                        existing.append(item)
            elif isinstance(v, str) and v not in existing:
                existing.append(v)
            mapping[k] = existing

    for key, shared_uid_list in mapping.items():
        if (isinstance(shared_uid_list, list) and shared_user_id in shared_uid_list) or shared_uid_list == shared_user_id:
            return key.lower()

    # Custom OEM sharedUserIds (e.g. android.uid.oplus.*, com.miui.*, android.uid.samsung.*) -> sign consistently with platform
    return "platform"


def get_signing_key_from_module(android_apk_file_path, firmware_id, cookies, aosp_path=None):
    file_name = os.path.basename(android_apk_file_path)
    module_name = file_name.split(".")[0]
    android_mk_file_path = os.path.join(EXTRACTED_PACKAGES_PATH, module_name, "Android.mk")
    if not os.path.exists(android_mk_file_path):
        android_mk_file_path = os.path.join(EXTRACTED_PACKAGES_PATH, module_name, "Android.bp")

    logging.debug(f"Android.mk/Android.bp file path: {android_mk_file_path}")

    signing_key = None
    if os.path.exists(android_mk_file_path):
        with open(android_mk_file_path, "r") as file:
            for line in file:
                if "LOCAL_CERTIFICATE" in line:
                    signing_key = line.split("=")[1].strip()
                elif "certificate:" in line:
                    signing_key = line.split(":")[1].strip().replace('"', '').replace("'", "").replace(",", "")
                if signing_key is not None:
                    if signing_key.upper() == "PRESIGNED":
                        break
                    return signing_key.lower()

    shared_user_id = get_shared_user_from_manifest(firmware_id, android_apk_file_path, cookies, aosp_path=aosp_path)
    if shared_user_id:
        resolved_key = _resolve_signing_key_for_shared_uid(shared_user_id)
        if resolved_key:
            logging.debug(f"Resolved sharedUserId '{shared_user_id}' for {android_apk_file_path} -> key '{resolved_key}'")
            return resolved_key

    return "platform"


def get_signing_key_path(aosp_path, signing_key_name):
    base = aosp_path or ""
    if base and not base.endswith("/"):
        base += "/"
    key_file_path = f"{base}build/target/product/security/{signing_key_name}.p12"
    key_file_path = key_file_path.replace("//", "/")
    return key_file_path


def align_apk_file(apk_file_path, aosp_path):
    logging.info(f"Align apk file: {apk_file_path}")
    out_file_path = f"{apk_file_path}.aligned"
    zipalign_cmd = get_zipalign_binary_command(aosp_path)
    for flags in (['-f', '-p', '4'], ['-f', '-P', '16', '4'], ['-f', '4']):
        command = [zipalign_cmd, *flags, apk_file_path, out_file_path]
        success, log_message = execute_command(command)
        if success and os.path.exists(out_file_path):
            shutil.move(out_file_path, apk_file_path)
            return True, log_message
    if os.path.exists(out_file_path):
        try:
            os.remove(out_file_path)
        except Exception:
            pass
    return False, log_message


def build_aosp_apk_index(target_out_path, aosp_path=None):
    """
    Scans existing APKs across system, system_ext, product, and vendor in target_out_path
    and returns:
      {
        "by_package": { package_name: [apk_file_path, ...] },
        "by_authority": { authority: [apk_file_path, ...] }
      }
    """
    index = {"by_package": {}, "by_authority": {}}
    if not target_out_path or not os.path.isdir(target_out_path):
        return index

    for part in ("system", "system_ext", "product", "vendor"):
        for sub in ("priv-app", "app"):
            base_dir = os.path.join(target_out_path, part, sub)
            if not os.path.isdir(base_dir):
                continue
            for apk_path in glob.glob(os.path.join(base_dir, "**", "*.apk"), recursive=True):
                info = inspect_apk_manifest_locally(apk_path, aosp_path=aosp_path)
                pkg = info.get("package_name")
                if pkg:
                    index["by_package"].setdefault(pkg, []).append(os.path.abspath(apk_path))
                for auth in info.get("provider_authorities", []):
                    index["by_authority"].setdefault(auth, []).append(os.path.abspath(apk_path))
    return index


def reconcile_and_deduplicate_vendor_apk(vendor_apk_path, target_apk_path, target_out_path, aosp_apk_index=None, aosp_path=None):
    """
    Checks whether injecting vendor_apk_path into target_apk_path conflicts with:
    1. PROTECTED_AOSP_PACKAGES_LIST (skip replacing critical emulator-coupled AOSP packages).
    2. Duplicate package_name or ContentProvider authority in another AOSP directory/partition
       (removes the conflicting AOSP APK directory so PackageManager does not abort or reject).
    Returns (allow_inject: bool, reason_or_none: str | None).
    """
    info = inspect_apk_manifest_locally(vendor_apk_path, aosp_path=aosp_path)
    pkg = info.get("package_name")
    if "__auto_generated_rro_" in str(vendor_apk_path) or (pkg and "__auto_generated_rro_" in pkg):
        return False, f"Skipping auto-generated vendor hardware RRO overlay ({vendor_apk_path})"

    overlay_target = info.get("overlay_target_package")
    if info.get("is_rro_overlay") and overlay_target in PROTECTED_RRO_TARGET_PACKAGES:
        return False, (
            f"Skipping vendor RRO overlay '{pkg or os.path.basename(vendor_apk_path)}' "
            f"targeting protected emulator package '{overlay_target}'"
        )

    if not pkg:
        return True, None

    protected_list = list(PROTECTED_AOSP_PACKAGES_LIST)
    if isinstance(POST_INJECTOR_CONFIG, dict):
        protected_list.extend(POST_INJECTOR_CONFIG.get("PROTECTED_AOSP_PACKAGES_LIST", []))

    if aosp_apk_index is None and target_out_path:
        aosp_apk_index = build_aosp_apk_index(target_out_path, aosp_path=aosp_path)

    existing_pkg_paths = (aosp_apk_index or {}).get("by_package", {}).get(pkg, [])
    if pkg in protected_list and existing_pkg_paths:
        return False, f"Protected AOSP core package '{pkg}' kept on AOSP baseline (skipped {vendor_apk_path})"

    target_abs = os.path.abspath(target_apk_path) if target_apk_path else ""
    conflicting_apks = set()
    for p in existing_pkg_paths:
        if os.path.abspath(p) != target_abs:
            conflicting_apks.add(os.path.abspath(p))

    for auth in info.get("provider_authorities", []):
        for p in (aosp_apk_index or {}).get("by_authority", {}).get(auth, []):
            if os.path.abspath(p) != target_abs:
                conflicting_apks.add(os.path.abspath(p))

    for conflict_apk in sorted(conflicting_apks):
        if not os.path.exists(conflict_apk):
            continue
        conflict_dir = os.path.dirname(conflict_apk)
        try:
            if os.path.basename(os.path.dirname(conflict_dir)) in ("priv-app", "app"):
                shutil.rmtree(conflict_dir)
                logging.info(
                    f"Removed conflicting duplicate AOSP app directory {conflict_dir} "
                    f"(package={pkg}) in favor of {vendor_apk_path}"
                )
            else:
                os.remove(conflict_apk)
                logging.info(f"Removed conflicting duplicate AOSP APK {conflict_apk} (package={pkg})")
        except Exception as e:
            logging.warning(f"Failed to remove conflicting AOSP APK {conflict_apk}: {e}")

    return True, None


def generate_privapp_permissions_allowlist(target_out_path, aosp_path=None):
    """
    Scans all APKs in <partition>/priv-app across system, system_ext, product, and vendor
    in target_out_path, extracts their package names and requested permissions from
    AndroidManifest.xml, and writes <partition>/etc/permissions/privapp-permissions-fmd-rehoster.xml
    so PermissionManagerService never crashes SystemServer under ro.control_privapp_permissions=enforce.
    Returns list of generated XML file paths.
    """
    generated_files = []
    if not target_out_path or not os.path.isdir(target_out_path):
        return generated_files

    for part in ("system", "system_ext", "product", "vendor"):
        priv_app_dir = os.path.join(target_out_path, part, "priv-app")
        if not os.path.isdir(priv_app_dir):
            continue

        pkg_permissions = {}
        for apk_path in sorted(glob.glob(os.path.join(priv_app_dir, "**", "*.apk"), recursive=True)):
            info = inspect_apk_manifest_locally(apk_path, aosp_path=aosp_path)
            pkg = info.get("package_name")
            perms = info.get("uses_permissions") or []
            if pkg and perms:
                existing = pkg_permissions.setdefault(pkg, [])
                for p in perms:
                    if p not in existing:
                        existing.append(p)

        if not pkg_permissions:
            continue

        etc_perm_dir = os.path.join(target_out_path, part, "etc", "permissions")
        os.makedirs(etc_perm_dir, exist_ok=True)
        xml_out_path = os.path.join(etc_perm_dir, "privapp-permissions-fmd-rehoster.xml")

        lines = [
            '<?xml version="1.0" encoding="utf-8"?>',
            '<permissions>',
        ]
        for pkg in sorted(pkg_permissions.keys()):
            lines.append(f'    <privapp-permissions package="{pkg}">')
            for perm in sorted(pkg_permissions[pkg]):
                lines.append(f'        <permission name="{perm}"/>')
            lines.append('    </privapp-permissions>')
        lines.append('</permissions>')
        lines.append('')

        with open(xml_out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        logging.info(
            f"Generated privapp-permissions allowlist for {len(pkg_permissions)} package(s) at {xml_out_path}"
        )
        generated_files.append(xml_out_path)

    return generated_files



def sign_apk_file(apk_file_path, signing_key_path, aosp_path, v2_signing_enabled=True, v3_signing_enabled=True,
                  v4_signing_enabled=True):
    """
    Signs the APK file with apksigner.
    """
    if not os.path.exists(apk_file_path):
        return False, f"Error: APK file not found for signing: {apk_file_path}"
    elif not os.path.exists(signing_key_path):
        return False, f"Error: Signing key not found for signing: {signing_key_path}"

    success = False
    log_message = ""
    apksigner_cmd_list = get_apksigner_binary_command(aosp_path)

    # Try different password formats that standard AOSP keys might use
    # Note: 'pass:' represents a truly empty password to Java/apksigner
    password_options = ['pass:', 'pass: ', 'pass:""',"pass:android"]

    for apksigner_cmd in apksigner_cmd_list:
        base_cmd = ["java", "-jar"] if ".jar" in apksigner_cmd else []

        for password in password_options:
            # 1. Build the full command with passwords
            sign_command = [
                *base_cmd,
                apksigner_cmd, 'sign',
                '--ks', signing_key_path,
                '--v2-signing-enabled', str(v2_signing_enabled).lower(),
                '--v3-signing-enabled', str(v3_signing_enabled).lower(),
                '--v4-signing-enabled', str(v4_signing_enabled).lower(),
                '--ks-pass', password,
                '--key-pass', password,
                '--verbose',
                '--in', apk_file_path,
                '--out', apk_file_path
            ]

            success, log_message = execute_command(sign_command)
            command_string = ' '.join(sign_command)

            logging.info(f"Trying password variation '{password}' for APK: {apk_file_path} - success: {success} - command_string: {command_string}")

            # 2. Fallback: If it fails, try without the password arguments
            if not success:
                logging.info(
                    f"Failed with password. Retrying without --ks-pass and --key-pass for APK: {apk_file_path}")

                # Slice out or filter out the password flags and their values
                fallback_command = [
                    arg for arg in sign_command
                    if arg not in ['--ks-pass', '--key-pass', password]
                ]

                success, log_message = execute_command(fallback_command)
                command_string = ' '.join(fallback_command)

                logging.info(f"Retry without passwords for APK: {apk_file_path} - success: {success}")

            # 3. Check for overall success (either from the main try or the fallback)
            if success:
                logging.info(f"Successfully signed APK file: {apk_file_path} using command: {command_string}")
                break

        if success:
            break

    return success, log_message

def verify_apk_file(apk_file_path, aosp_path):
    logging.info(f"Verifying APK file: {apk_file_path}")
    success = False
    log_message = ""
    apksigner_cmd_list = get_apksigner_binary_command(aosp_path)
    for apksigner_cmd in apksigner_cmd_list:
        base_cmd = ["java", "-jar"] if ".jar" in apksigner_cmd else []
        verify_command = [*base_cmd, apksigner_cmd, 'verify', apk_file_path]
        success, log_message = execute_command(verify_command)
        if success:
            break
    return success, log_message


def sign_apex_container_apksigner(apex_file_path,
                                    signing_key_path,
                                    signing_key_certificate_path,
                                    aosp_path,
                                    v2_signing_enabled=True,
                                    v3_signing_enabled=True,
                                    v4_signing_enabled=True):
    """
    Signs the APEX file with apksigner.

    :param apex_file_path: str - path to the APK file.
    :param signing_key_path: str - path to the signing key.
    :param signing_key_certificate_path: str - path to the signing certificate (pem).
    :param v4_signing_enabled: bool - enable v4 signing.
    :param v3_signing_enabled: bool - enable v3 signing.
    :param v2_signing_enabled: bool - enable v2 signing.

    """
    success = False
    log_message = ""
    apksigner_cmd_list = get_apksigner_binary_command(aosp_path)
    for apksigner_cmd in apksigner_cmd_list:
        base_cmd = ["java", "-jar"] if ".jar" in apksigner_cmd else []
        sign_command = [*base_cmd,
                        apksigner_cmd, 'sign',
                        '--key', signing_key_path,
                        '--cert', signing_key_certificate_path,
                        '--v2-signing-enabled', str(v2_signing_enabled).lower(),
                        '--v3-signing-enabled', str(v3_signing_enabled).lower(),
                        '--v4-signing-enabled', str(v4_signing_enabled).lower(),
                        '--verbose',
                        '--in', apex_file_path,
                        '--out', apex_file_path]
        success, log_message = execute_command(sign_command)
        logging.info(f"Signing APEX container file: {apex_file_path} "
                     f"with key: {signing_key_path} - {success} - {log_message} "
                     f"- sign_command: {sign_command}")
        if not success:
            base_cmd = ["java", "-jar"] if ".jar" in apksigner_cmd else []
            sign_command = [*base_cmd,
                            apksigner_cmd, 'sign',
                            '--key', signing_key_path,
                            '--cert', signing_key_certificate_path,
                            '--v2-signing-enabled', str(v2_signing_enabled).lower(),
                            '--v3-signing-enabled', str(v3_signing_enabled).lower(),
                            '--v4-signing-enabled', str(v4_signing_enabled).lower(),
                            '--key-pass', 'pass:android',
                            '--verbose',
                            '--in', apex_file_path,
                            '--out', apex_file_path]
            success, log_message = execute_command(sign_command)
            logging.info(f"Signing APEX container file: {apex_file_path} "
                         f"with key: {signing_key_path} - {success} - {log_message} "
                         f"- sign_command: {sign_command}")
        if success:
            break
    return success, log_message


def sign_apex_container_signapk(apex_file_path,
                        signing_key_path,
                        signing_key_certificate_path,
                        aosp_path,
                        lunch_target):
    """
    java \
      -Djava.library.path=$(dirname out/host/linux-x86/lib64/libconscrypt_openjdk_jni.so)\
      -jar out/host/linux-x86/framework/signapk.jar \
      -a 4096 \
      <apk_certificate_file> \
      <apk_private_key_file> \
      <unsigned_input_file> \
      <signed_output_file>

    """
    if not os.path.exists(signing_key_path) or not os.path.exists(signing_key_certificate_path):
        return False, f"APEX ignoring key or certificate not found: {signing_key_path} - {signing_key_certificate_path}"
    elif not os.path.exists(apex_file_path):
        return False, f"APEX file not found for signing: {apex_file_path}"
    elif not os.path.exists(aosp_path):
        return False, f"AOSP path not found for signing: {aosp_path}"

    current_directory = os.path.dirname(os.path.realpath(__file__))
    os.chdir(aosp_path)

    try:
        apex_out_file_path = f"{apex_file_path}.signed"
        #env_setup_command = f"bash -c 'cd {aosp_path} && source {aosp_path}build/envsetup.sh && lunch {lunch_target} && "
        #sign_command = env_setup_command +  f"java -Djava.library.path={aosp_path}out/host/linux-x86/lib64/ " \
        sign_command = f"java -Djava.library.path={aosp_path}out/host/linux-x86/lib64/ " \
                                           f"-jar out/host/linux-x86/framework/signapk.jar " \
                                            f"--min-sdk-version 28 " \
                                            f"-a 4096 " \
                                            f"{signing_key_certificate_path} " \
                                            f"{signing_key_path} " \
                                            f"{apex_file_path} " \
                                            f"{apex_out_file_path}"
        success, log_message = execute_command(sign_command, cwd=aosp_path, shell=True)
        logging.info(f"Signed APEX container file: {apex_file_path} "
                     f"with key: {signing_key_path} - {success} - {log_message} "
                     f"- sign_command: {sign_command}")
        if success:
            logging.info(f"Moving signed APEX container file: {apex_out_file_path} to {apex_file_path}")
            shutil.move(apex_out_file_path, apex_file_path)
            success = True
            log_message = None
        else:
            log_message = f"Error signing APEX container file: {apex_file_path} with key: {signing_key_path} - {log_message}"

        if os.path.exists(apex_out_file_path):
            logging.info(f"Removing signed APEX container file: {apex_out_file_path}")
            os.remove(apex_out_file_path)

    except Exception as e:
        success = False
        traceback.print_exc()
        log_message = f"Error signing APEX container file: {apex_file_path} with key: {signing_key_path} - {e}"
    finally:
        os.chdir(current_directory)
    return success, log_message