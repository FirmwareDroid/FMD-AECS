import os
import shutil
import subprocess
import re
import argparse
from typing import Optional, Sequence
import logging

# Configuration: Update these paths to match your environment
AOSP_PATHS = {
    "11": "~/aosp/aosp11",
    "12": "~/aosp/aosp12",
    "12_1": "~/aosp/aosp12_1",
    "13": "~/aosp/aosp13",
    "14": "~/aosp/aosp14",
    "15": "~/aosp2/aosp15",
    "16": "~/aosp2/aosp16",
}
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
BUILD_IMAGE_SCRIPT_PATH = "./build/make/tools/releasetools/build_image.py"


def run_command(cmd: str, cwd: Optional[str] = None, dry_run: bool = False, verbose: bool = False):
    """Executes a shell command. Honors dry_run and verbose flags."""
    if dry_run or verbose:
        logging.info("[CMD] %s (cwd=%s)", cmd, cwd)
    if dry_run:
        return
    try:
        subprocess.run(cmd, shell=True, check=True, cwd=cwd, executable='/bin/bash')
    except subprocess.CalledProcessError as e:
        logging.error('Error executing: %s\n%s', cmd, e)


def file_has_any(full_path: str, substrings: Sequence[str]) -> bool:
    """Return True if the file contains any of the given substrings.

    Non-fatal: if the file doesn't exist, returns False.
    """
    try:
        with open(os.path.expanduser(full_path), 'r') as f:
            content = f.read()
    except Exception:
        return False
    for s in substrings:
        if s in content:
            return True
    return False


def modify_file(file_path: str, search_pattern: str, replacement: str, append: bool = False,
                flags: int = 0, dry_run: bool = False, verbose: bool = False):
    """Replaces text in a file or appends to the end.

    - flags is passed to re.sub
    - dry_run: if True, do not write changes, only print what would be done
    """
    full_path = os.path.expanduser(file_path)
    if not os.path.exists(full_path):
        logging.warning("Warning: File not found: %s", full_path)
        return

    with open(full_path, 'r') as f:
        content = f.read()

    if append:
        if replacement not in content:
            if dry_run or verbose:
                logging.info("[MODIFY] Append to %s:\n%s\n", full_path, replacement)
            if not dry_run:
                with open(full_path, 'a') as f:
                    f.write(f"\n{replacement}\n")
    else:
        new_content = re.sub(search_pattern, replacement, content, flags=flags)
        if new_content != content:
            if dry_run or verbose:
                logging.info("[MODIFY] Update %s: pattern=%r", full_path, search_pattern)
            if not dry_run:
                with open(full_path, 'w') as f:
                    f.write(new_content)


def setup_certificates(version, base_path, dry_run: bool = False, verbose: bool = False):
    """Generates PEM/P12 keys from AOSP PK8 files."""
    sec_path = os.path.join(base_path, "build/target/product/security")
    logging.info('--- Processing Certificates for Android %s ---', version)

    keys = ["platform", "media", "networkstack", "shared", "testkey"]
    if version in ["12", "12_1"]:
        keys.append("verity")
    if version in ["13", "14", "15", "16"]:
        keys.append("bluetooth")
    if version == "16":
        keys.extend(["nfc", "sdk_sandbox", "cts_uicc_2021"])

    for key in keys:
        if version in ["12", "12_1"]:
            cmd = (f"openssl pkcs8 -in {key}.pk8 -inform DER -nocrypt -out {key}.pem && "
                   f"openssl pkcs12 -export -in {key}.x509.pem -inkey {key}.pem "
                   f"-out {key}.p12 -name {key} -passout pass: -legacy")
        else:
            cmd = (f"openssl pkcs8 -in {key}.pk8 -inform DER -nocrypt -out {key}.pem && "
                   f"openssl pkcs12 -export -in {key}.x509.pem -inkey {key}.pem "
                   f"-out {key}.p12 -name {key} -passout pass:")
        run_command(cmd, cwd=os.path.expanduser(sec_path), dry_run=dry_run, verbose=verbose)

    if version in ["12", "12_1"]:
        cmd = (f"cp ./platform.pem ./bluetooth.pem "
               f"&& cp ./platform.pk8 ./bluetooth.pk8 "
               f"&& cp ./platform.x509.pem ./bluetooth.x509.pem "
               f"&& cp ./platform.p12 ./bluetooth.p12")
        run_command(cmd, cwd=os.path.expanduser(sec_path), dry_run=dry_run, verbose=verbose)


def disable_selinux(base_path, dry_run: bool = False, verbose: bool = False):
    """Forces SELinux to Permissive in C++ source."""
    logging.info('--- Disable Selinux ---')
    target = os.path.join(base_path, "system/core/init/selinux.cpp")
    # Skip if the file already contains our permissive replacement
    if file_has_any(target, ["EnforcingStatus StatusFromProperty() { return SELINUX_PERMISSIVE;", "IsEnforcing() { return false; "]):
        if verbose or dry_run:
            logging.info('[SKIP] SELinux already configured in: %s', target)
        return

    # Replace the body of StatusFromProperty and IsEnforcing
    # This is a simplified replacement; logic can be tuned for regex accuracy
    modify_file(target, r"EnforcingStatus status = SELINUX_ENFORCING;",
                "return SELINUX_PERMISSIVE;\n    EnforcingStatus status = SELINUX_ENFORCING;\n",
                flags=re.DOTALL, dry_run=dry_run, verbose=verbose)
    modify_file(target, r"if \(ALLOW_PERMISSIVE_SELINUX\) \{",
                "return false;\n    if (ALLOW_PERMISSIVE_SELINUX) {\n ",
                flags=re.DOTALL, dry_run=dry_run, verbose=verbose)


def ensure_apksigner(verbose: bool = False, dry_run: bool = False):
    """Check for apksigner availability and warn if missing.

    When dry_run is True, do not execute any subprocess; just log the intended check.
    """
    if dry_run:
        # In dry-run mode we always log that we would perform this check.
        logging.info("[DRY-RUN] Would check for 'apksigner' in PATH")
        return
    try:
        subprocess.run(["apksigner", "--version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if verbose:
            logging.info('apksigner found in PATH')
    except Exception:
        logging.warning("Warning: 'apksigner' not found in PATH. Install it (e.g. 'sudo apt install apksigner') if you need APK signing.")


def add_boardconfig_flags(version: str, base_path: str, dry_run: bool = False, verbose: bool = False):
    """Append BUILD_BROKEN_DUP_RULES and SELINUX_IGNORE_NEVERALLOWS to appropriate BoardConfig.mk files."""
    logging.info('--- Add Board config Flags ---')
    paths = []
    if version in ("12", "12_1"):
        paths = [
            "build/target/board/emulator_arm64/BoardConfig.mk",
            "build/target/board/emulator_arm/BoardConfig.mk",
        ]
    elif version == "13":
        paths = ["build/target/board/emulator_arm64/BoardConfig.mk"]
    elif version == "14":
        paths = ["build/target/board/generic_arm64/BoardConfig.mk"]
    elif version in ("15", "16"):
        paths = ["build/target/board/generic_arm64/BoardConfig.mk"]

    for p in paths:
        modify_file(os.path.join(base_path, p), "",
                    "BUILD_BROKEN_DUP_RULES := true\nSELINUX_IGNORE_NEVERALLOWS := true\n}",
                    append=True, dry_run=dry_run, verbose=verbose)

    if version in ("12", "12_1"):
        paths = [
            "device/generic/goldfish/emulator_arm64/BoardConfig.mk",
        ]
    elif version == ["13", "14"]:
        paths = ["device/generic/goldfish/emulator_arm64/BoardConfig.mk" 
                 "device/generic/goldfish/emu64a/BoardConfig.mk"]
    elif version in ("15", "16"):
        paths = ["device/generic/goldfish/board/emu64a//BoardConfig.mk"]

    for p in paths:
        try:
            modify_file(os.path.join(base_path, p), "",
                        "BOARD_KERNEL_CMDLINE += androidboot.selinux=permissive\n",
                        append=True, dry_run=dry_run, verbose=verbose)
        except Exception:
            logging.warning("Warning: '%s' not found in PATH", p)


def add_build_properties(version: str, base_path: str, dry_run: bool = False, verbose: bool = False):
    """Add property overrides and other build flags to product/vendor files as required."""
    logging.info('--- Add Board Properties ---')
    targets = []
    if version in ("12", "12_1"):
        targets = [
            "build/target/product/sdk_phone_arm64.mk",
            "build/target/product/emulator.mk",
            "device/generic/goldfish/64bitonly/product/vendor.mk",
            "device/generic/goldfish/vendor.mk",
        ]
        if version == "12_1":
            targets += [
                "build/target/product/sdk_phone_arm64.mk",
                "build/target/product/emulator.mk",
            ]
    elif version == "13":
        targets = [
            "device/generic/goldfish/64bitonly/product/vendor.mk",
            "device/generic/goldfish/vendor.mk",
        ]
    elif version == "14":
        targets = [
            "device/generic/goldfish/product/generic.mk",
            "device/generic/goldfish/vendor.mk",
        ]
    elif version in ("15", "16"):
        targets = ["device/generic/goldfish/product/generic.mk"]

    append_block = (
        "TARGET_SUPPORTS_32_BIT_APPS := true\n"
        "TARGET_SUPPORTS_64_BIT_APPS := true\n"
        "PRODUCT_PROPERTY_OVERRIDES += ro.control_privapp_permissions?=log\n"
        "MODULE_BUILD_FROM_SOURCE := true\n"
        "PRODUCT_PROPERTY_OVERRIDES += ro.sf.lcd_density=240\n",
        "BUILD_BROKEN_SRC_DIR_IS_WRITABLE := true"  # Android 14 and above only
    )

    for t in targets:
        target_path = os.path.join(base_path, t)
        # If the file already contains our canonical override, skip configuration for this file
        try:
            with open(os.path.expanduser(target_path), 'r') as f:
                content = f.read()
        except Exception:
            content = ''
        if 'PRODUCT_PROPERTY_OVERRIDES += ro.control_privapp_permissions?=log' in content:
            if verbose or dry_run:
                logging.info('[SKIP] Build properties already present in: %s', target_path)
                continue
        # If a rule exists that sets the property to 'enforce', replace it with the safer '?=log' form
        #  - handle the common form: PRODUCT_PROPERTY_OVERRIDES += ro.control_privapp_permissions=enforce
        modify_file(target_path,
                    r"PRODUCT_PROPERTY_OVERRIDES\s*\+=\s*ro\.control_privapp_permissions\s*(?:\?|:)?=\s*enforce",
                    "PRODUCT_PROPERTY_OVERRIDES += ro.control_privapp_permissions?=log",
                    flags=re.MULTILINE, dry_run=dry_run, verbose=verbose)
        #  - also handle any bare assignment like ro.control_privapp_permissions=enforce (replace by the override line)
        modify_file(target_path,
                    r"ro\.control_privapp_permissions\s*(?:\?|:)?=\s*enforce",
                    "PRODUCT_PROPERTY_OVERRIDES += ro.control_privapp_permissions?=log",
                    flags=re.MULTILINE, dry_run=dry_run, verbose=verbose)

        # Ensure the other helpful build flags are present; append_block contains the override too
        block_as_string = "".join(append_block)
        modify_file(target_path, "", block_as_string, append=True, dry_run=dry_run, verbose=verbose)


def inject_build_image_script(version: str, build_image_path: str, full_path: str):
    """
    Overwrites the AOSP build_image.py script with a custom version that starts the post-injector build.
    """
    if full_path:
        aosp_path = full_path
    else:
        aosp_path = AOSP_PATHS.get(version)

    if not aosp_path:
        raise ValueError(f"AOSP version '{version}' is not configured in AOSP_PATHS.")

    overwrite_path = os.path.join(str(aosp_path), str(BUILD_IMAGE_SCRIPT_PATH))
    overwrite_path = os.path.expanduser(overwrite_path)
    overwrite_path = os.path.normpath(overwrite_path)
    overwrite_path = os.path.abspath(overwrite_path)

    if not os.path.exists(overwrite_path):
        raise FileNotFoundError(f"build_image.py path: {overwrite_path} does not exist")

    try:
        shutil.copyfile(build_image_path, overwrite_path)
        logging.info(f"Successfully injected custom build_image.py into {overwrite_path}")
    except Exception as e:
        logging.error(f"Error injecting build_image.py: {e}")
        raise  # Re-raise exception so the script knows it failed


def disable_platform_tests(version: str, base_path: str, dry_run: bool = False, verbose: bool = False):
    """Comment out specific platform tests (ApiDemos, BusinessCard)"""
    logging.info('--- Disable certain platform tests ---')
    path = os.path.join(base_path, "platform_testing/build/tasks/tests/platform_test_list.mk")
    # Comment out ApiDemos and BusinessCard lines
    modify_file(path, r"^\s*ApiDemos \\", r"    #ApiDemos \\", flags=re.MULTILINE, dry_run=dry_run, verbose=verbose)
    modify_file(path, r"^\s*BusinessCard \\", r"    #BusinessCard \\", flags=re.MULTILINE, dry_run=dry_run, verbose=verbose)


def disable_boringssl_checks(version: str, base_path: str, dry_run: bool = False, verbose: bool = False):
    """Disable reboot_on_failure and add BORINGSSL env where appropriate."""

    logging.info('--- Disable BoringSSL checks ---')
    # init.rc modifications (may not exist for all versions)
    init_rc = os.path.join(base_path, "system/core/rootdir/init.rc")
    if not os.path.exists(init_rc):
        raise FileNotFoundError(init_rc)
    if file_has_any(init_rc, ["#reboot_on_failure reboot,boringssl-self-check-failed"]):
        if verbose or dry_run:
            logging.info('[SKIP] boringssl reboot_on_failure already commented in: %s', init_rc)
    else:
        modify_file(init_rc, r"reboot_on_failure\s+reboot,boringssl-self-check-failed", r"#reboot_on_failure reboot,boringssl-self-check-failed", flags=0, dry_run=dry_run, verbose=verbose)

    # boringssl self test rc files
    candidates = [
        os.path.join(base_path, "external/boringssl/selftest/boringssl_self_test.rc"),
    ]
    for c in candidates:
        if not os.path.exists(c):
            raise FileNotFoundError("Expected boringssl self test rc file not found: %s", c)
        if file_has_any(c, ["#reboot_on_failure reboot,boringssl-self-check-failed"]):
            if verbose or dry_run:
                logging.info('[SKIP] boringssl reboot_on_failure already commented in: %s', c)
        else:
            modify_file(c, r"reboot_on_failure\s+reboot,boringssl-self-check-failed", r"#reboot_on_failure reboot,boringssl-self-check-failed", flags=0, dry_run=dry_run, verbose=verbose)


def disable_vndk_checks(version: str, base_path: str, dry_run: bool = False, verbose: bool = False):
    """Append common VNDK exceptions to vndk.go for older Android versions."""
    if version not in ("11", "12", "12_1", "13", "14"):
        return
    path = os.path.join(base_path, "build/soong/cc/config/vndk.go")
    block = (
        "\n// Added by aosp_setup to relax VNDK checks\n"
        "        \"libjpeg\",\n"
        "        \"libwifi-system-iface\",\n"
        "        \"libnl\",\n"
        "        \"libvndksupport\",\n"
        "        \"libhardware_legacy\",\n"
        "        \"android.hardware.media.omx@1.0\",\n"
        "        \"android.hardware.media.omx@2.0\",\n"
        "        \"android.hardware.media.omx@3.0\",\n"
    )
    modify_file(path, "", block, append=True, dry_run=dry_run, verbose=verbose)


def add_privapp_permission(version: str, base_path: str, dry_run: bool = False, verbose: bool = False):
    """Ensure launcher3 PACKAGE_USAGE_STATS permission is present for older versions."""
    if version not in ("11", "12", "12_1", "13"):
        return
    path = os.path.join(base_path, "frameworks/base/data/etc/privapp-permissions-platform.xml")
    permission_line = '<permission name="android.permission.PACKAGE_USAGE_STATS"/>'
    modify_file(path, "", permission_line, append=True, dry_run=dry_run, verbose=verbose)


def disable_build_tests(version: str, base_path: str, dry_run: bool = False, verbose: bool = False):
    """Comment out TvSystemUITests in test lists for newer versions where applicable."""
    logging.info('--- Disable Platform TVSystemUITests ---')
    if version == "14":
        path = os.path.join(base_path, "platform_testing/build/tasks/tests/instrumentation_test_list.mk")
    elif version in ["15"]:
        path = os.path.join(base_path, "platform_testing/build/tasks/tests/instrumentation_test_list.mk")
    elif version in ["16"]:
        path = os.path.join(base_path, "platform_testing/Android.bp")
    else:
        return
    if dry_run:
        logging.info('--- DRY RUN changes file: %s ---', path)
        return
    modify_file(path, r"TvSystemUITests \\", r"#TvSystemUITests \\", flags=0, dry_run=dry_run, verbose=verbose)

def disable_avatarpicker(version: str, base_path: str, dry_run: bool = False, verbose: bool = False):
    """Comment out Avatarpicker """
    logging.info('--- Disable Platform Avatarpicker ---')
    if version in ["15"]:
        path = os.path.join(base_path, "build/make/target/product/generic_system.mk")
        path2 = os.path.join(base_path, "device/google/cuttlefish/system_image/Android.bp")
    else:
        return
    if dry_run:
        logging.info('--- DRY RUN changes file: %s ---', path)
        return
    modify_file(path, r"AvatarPicker", r"#AvatarPicker", flags=0, dry_run=dry_run, verbose=verbose)
    modify_file(path2, r'"AvatarPicker"', r'//"AvatarPicker"', flags=0, dry_run=dry_run, verbose=verbose)


def disable_eyedropper(version: str, base_path: str, dry_run: bool = False, verbose: bool = False):
    """Comment out EyeDropper """
    logging.info('--- Disable EyeDropper ---')
    if version in ["16"]:
        path = os.path.join(base_path, "build/make/target/product/generic/Android.bp")
    else:
        return
    if dry_run:
        logging.info('--- DRY RUN changes file: %s ---', path)
        return
    # Comment out the EyeDropper entry (replace "EyeDropper" with #"EyeDropper")
    modify_file(path, r'"EyeDropper"', r'// EyeDropper"', flags=0, dry_run=dry_run, verbose=verbose)


def generate_missing_dns_keys(version: str, base_path: str, dry_run: bool = False, verbose: bool = False):
    """Generate missing testcert keys for DNSResolver APEX and extract public key using avbtool if available."""
    logging.info('--- Setup DNS Resolver APEX ---')
    # location for DnsResolver apex
    dns_apex = os.path.join(base_path, "packages/modules/DnsResolver/apex")
    if not os.path.exists(dns_apex):
        return
    cmds = []
    cmds.append("openssl pkcs8 -inform DER -in testcert.pk8 -out testcert.pem -nocrypt")
    cmds.append("cp testcert.pem com.android.resolv.pem")
    cmds.append("cp testcert.pk8 com.android.resolv.pk8")
    cmds.append("cp testcert.x509.pem com.android.resolv.x509.pem")
    # avbtool location varies; try a few common locations
    avbtool_paths = [
        os.path.join(base_path, "external/avb/avbtool"),
        os.path.join(base_path, "out/host/linux-x86/bin/avbtool"),
        "/usr/bin/avbtool",
    ]
    avb = next((p for p in avbtool_paths if os.path.exists(p)), None)
    if avb:
        cmds.append(f"{avb} extract_public_key --key testcert.pem --output com.android.resolv.avbpubkey")

    if dry_run:
        logging.info('[DRY-RUN] Would execute command for DNS Apex Key Generation: %s', cmds)
        return

    for c in cmds:
        run_command(c, cwd=dns_apex, dry_run=dry_run, verbose=verbose)

    dns_apex_android_bp = os.path.join(base_path, "packages/modules/DnsResolver/apex/Android.bp")
    if dry_run:
        logging.info('Replace DNS Resolve testcert with newly generated certificate: %s', dns_apex_android_bp)
        return
    modify_file(dns_apex_android_bp, r"\"testcert\",", r'"com.android.resolv"', flags=0, dry_run=dry_run, verbose=verbose)




def disable_cleango(version: str, base_path: str, dry_run: bool = False, verbose: bool = False):
    """Disable cleanOldFiles behaviour by returning early in cleanbuild.go."""
    logging.info('--- Disable cleanOldFiles behaviour by returning early in cleanbuild.go. ---')
    if dry_run:
        logging.info('[SKIP] cleanOldFiles dry-run')
        return

    cleango = os.path.join(base_path, "build/soong/ui/build/cleanbuild.go")
    replacement = "newFile = filepath.Join(basePath, newFile)\n        return\n"
    modify_file(cleango, r"newFile = filepath.Join\(basePath, newFile\)", replacement, flags=re.DOTALL, dry_run=dry_run, verbose=verbose)

    testgo = os.path.join(base_path, "build/soong/ui/build/cleanbuild_test.go")
    if version in ["12", "12_1"]:
        replacement2 = "dir, err := ioutil.TempDir(\"\", \"testcleanoldfiles\")\n        return\n"
    else:
        replacement2 = "dir, err := os.MkdirTemp(\"\", \"test-clean-old-files\")\n        return\n"
    modify_file(testgo, r"dir, err := ioutil.TempDir\(\"\", \"testcleanoldfiles\"\)", replacement2, flags=re.DOTALL, dry_run=dry_run, verbose=verbose)


def disable_dex_preopt(version: str, base_path: str, dry_run: bool = False, verbose: bool = False):
    logging.info('--- Disable WITH_DEXPREOPT ---')
    if version not in ["14"]:
        return
    path = os.path.join(base_path, "build/make/core/board_config.mk")
    replacement = "WITH_DEXPREOPT := false\n"
    if dry_run:
        logging.info('--- Disable Dex Preopt by adding WITH_DEXPREOPT := false to %s ---', path)
        return
    modify_file(path, r"WITH_DEXPREOPT := true", replacement, flags=0, dry_run=dry_run, verbose=verbose)


def disable_zygote_onrestart(base_path: str, dry_run: bool = False, verbose: bool = False):
    """Disable the 'onrestart restart zygote' line in init.zygote64_32.rc.

    This function comments out the exact line 'onrestart restart zygote' in
    system/core/rootdir/init.zygote64_32.rc so zygote won't be auto-restarted by init.

    Usage: disable_zygote_onrestart(full_aosp_path, dry_run=True)
    """
    rc_path = os.path.join(base_path, "system/core/rootdir/init.zygote64_32.rc")
    # Match the line with optional leading whitespace and comment it out.
    pattern = r"^\s*onrestart\s+restart\s+zygote\s*$"
    replacement = "# onrestart restart zygote"
    modify_file(rc_path, pattern, replacement, flags=re.MULTILINE, dry_run=dry_run, verbose=verbose)



def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AOSP repository tweaks and helpers")
    parser.add_argument(
        "-V", "--version",
        default=list(AOSP_PATHS.keys())[-1],
        choices=list(AOSP_PATHS.keys()),
        help="A single AOSP version to process (defaults to the newest known version)"
    )
    parser.add_argument(
        "-p", "--path",
        help="Path to the AOSP repository base. If provided, this overrides the internal AOSP_PATHS mapping for the selected version.")
    parser.add_argument("-b", "--build-image-script-path", default=None, help="Path to the custom build_image.py script to inject", required=True)
    # The script is intended to run all steps; skipping individual steps was removed.
    parser.add_argument("--dry-run", action="store_true", help="Print actions without making changes")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    return parser.parse_args(argv)


def main(args: argparse.Namespace):
    ver = args.version
    # If the user provided an explicit path, use that (overrides AOSP_PATHS mapping)
    if getattr(args, "path", None):
        full_path = os.path.expanduser(args.path)
        if not os.path.exists(full_path):
            logging.error('Provided path does not exist: %s', full_path)
            return
        # keep a `path` variable for backwards-compatible messaging (was used below)
        path = full_path
    else:
        path = AOSP_PATHS.get(ver)
        if path is None:
            logging.error('Unknown AOSP version: %s', ver)
            return

        full_path = os.path.expanduser(path)
        if not os.path.exists(full_path):
            logging.error('Path for version %s does not exist: %s', ver, full_path)
            return

    build_image_path = args.build_image_script_path
    if not os.path.exists(build_image_path):
        raise FileNotFoundError(f"Provided build_image.py path does not exist: {build_image_path}")

    logging.info('=== Customizing AOSP %s at %s ===', ver, path)
    # Ensure apksigner available (warn if not). Respect dry-run.
    ensure_apksigner(verbose=args.verbose, dry_run=args.dry_run)

    # 1. Certificates
    setup_certificates(ver, full_path, dry_run=args.dry_run, verbose=args.verbose)

    # 2. BoardConfig Mods (Duplicates & Neverallows) - version-specific
    add_boardconfig_flags(ver, full_path, dry_run=args.dry_run, verbose=args.verbose)

    # 2b. Add build properties where needed
    add_build_properties(ver, full_path, dry_run=args.dry_run, verbose=args.verbose)

    # 3. Disable SELinux in Code
    disable_selinux(full_path, dry_run=args.dry_run, verbose=args.verbose)

    # 4. Artifact Path Requirements
    logging.info('--- Disable Artifact Path Requirements ---')
    system_mk = "build/make/target/product/generic_system.mk"
    modify_file(os.path.join(full_path, system_mk),
                r"\$\(call require-artifacts-in-path", r"# $(call require-artifacts-in-path",
                dry_run=args.dry_run, verbose=args.verbose)

    # 5. Disable APEX Compression (sed logic)
    logging.info('--- Disable APEX Compression ---')
    # First, list files that contain the pattern so verbose output can show what will be changed
    list_cmd = "find . -name 'Android.bp' -exec grep -l \"compressible: true,\" {} + || true"
    try:
        res = subprocess.run(list_cmd, shell=True, check=False, cwd=full_path, executable='/bin/bash', stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        files = [f for f in res.stdout.splitlines() if f]
    except Exception:
        files = []

    if args.verbose:
        if files:
            logging.info("[INFO] Android.bp files containing 'compressible: true,':")
            for f in files:
                logging.info('  %s', f)
        else:
            logging.info("[INFO] No Android.bp files with 'compressible: true,' found")

    # Now perform the replacement (run_command will honor dry_run)
    run_command("find . -name 'Android.bp' -exec sed -i 's/compressible: true,/compressible: false,/g' {} +",
                cwd=full_path, dry_run=args.dry_run, verbose=args.verbose)

    # 6. Disable certain platform tests
    disable_platform_tests(ver, full_path, dry_run=args.dry_run, verbose=args.verbose)

    # 7. Disable boringssl checks (comment reboot_on_failure)
    disable_boringssl_checks(ver, full_path, dry_run=args.dry_run, verbose=args.verbose)

    # 8. Disable VNDK checks for older versions
    #disable_vndk_checks(ver, full_path, dry_run=args.dry_run, verbose=args.verbose)

    # 9. Add privapp permission for Launcher3 where applicable
    #add_privapp_permission(ver, full_path, dry_run=args.dry_run, verbose=args.verbose)


    # 10. Generate missing keys for DNSResolver apex
    generate_missing_dns_keys(ver, full_path, dry_run=args.dry_run, verbose=args.verbose)

    # 11. Disable cleanOldFiles behaviour in soong's cleanbuild
    disable_cleango(ver, full_path, dry_run=args.dry_run, verbose=args.verbose)

    disable_build_tests(ver, full_path, dry_run=args.dry_run, verbose=args.verbose)
    disable_avatarpicker(ver, full_path, dry_run=args.dry_run, verbose=args.verbose)
    disable_eyedropper(ver, full_path, dry_run=args.dry_run, verbose=args.verbose)
    disable_zygote_onrestart(full_path, dry_run=args.dry_run, verbose=args.verbose)
    #disable_dex_preopt(ver, full_path, dry_run=args.dry_run, verbose=args.verbose)

    inject_build_image_script(ver, build_image_path, full_path)
    


if __name__ == "__main__":
    args = parse_args()
    main(args)
