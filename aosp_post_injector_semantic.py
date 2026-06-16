import glob
import logging
import os
import re
import shutil
import tempfile

from aosp_sepolicy_merger import merge_sepolicy_pipeline
from aosp_vintf_handler import merge_vintf_artifacts
from common import get_aosp_build_out_dir

VINTF_FOLDER_RELATIVE_PATH = "etc/vintf"
ASSEMBLE_VINTF_PATH_LIST = ["out/host/linux-x86/bin/assemble_vintf"]
SECIL_BINARY_PATH_LIST = ["./out/host/linux-x86/bin/secilc", "out/soong/.intermediates/external/selinux/secilc/secilc"]

def get_aosp_vintf_path(aosp_emulator_out_dir: str, partition_name: str):
    vintf_folder_path = os.path.join(aosp_emulator_out_dir, partition_name, VINTF_FOLDER_RELATIVE_PATH)
    if not os.path.exists(vintf_folder_path) and not os.path.isdir(vintf_folder_path):
        raise FileNotFoundError(f"VINTF folder not found at expected path: {vintf_folder_path}")
    logging.info(f"Found aosp vintf folder: {vintf_folder_path}")
    return vintf_folder_path


def get_vintf_vendor_path(partition_name: str, vintf_folder_path_list: list):
    # Extract base dirs, check if partition_name is part of the path segments, and deduplicate
    base_dirs = list(dict.fromkeys(
        path.split('/etc/vintf')[0]
        for path in vintf_folder_path_list
        if partition_name in path.split('/etc/vintf')[0].split('/')
    ))
    if len(base_dirs) == 1:
        vintf_root_dir = base_dirs[0]
        logging.info(f"Found vintf root directory: {vintf_root_dir}")
    else:
        logging.warning(f"Found multiple vintf root directories: {base_dirs}")
        raise RuntimeError(f"Found multiple vintf root directories: {base_dirs}")

    return vintf_root_dir


def _find_binary_dir(aosp_path, path_list, binary_name):
    """Helper function to find a binary's directory from a list of candidate paths."""
    for candidate_path in path_list:
        bin_path = os.path.join(aosp_path, candidate_path)
        if os.path.exists(bin_path):
            bin_dir = os.path.dirname(bin_path)
            logging.info(f"Found {binary_name} folder: {bin_dir}")
            return str(bin_dir)

    raise FileNotFoundError(
        f"Could not find '{binary_name}' in expected paths: {path_list}"
    )


def find_assemble_vintf(aosp_path):
    return _find_binary_dir(aosp_path, ASSEMBLE_VINTF_PATH_LIST, "assemble_vintf")


def find_secil_binary(aosp_path):
    return _find_binary_dir(aosp_path, SECIL_BINARY_PATH_LIST, "secilc")



def handle_vintf_merge(aosp_path: str, aosp_version:str, partition_name: str, vintf_folder_path_list: list):
    """
    Merges two vintf folders into one folder.
    """
    logging.info(f"Merging vintf folder: {aosp_path}, version: {aosp_version}, partition: {partition_name}, vintf_folder_path_list: {vintf_folder_path_list}")
    aosp_emulator_out_dir = get_aosp_build_out_dir(aosp_path, aosp_version)
    aosp_vintf_dir = get_aosp_vintf_path(aosp_emulator_out_dir, partition_name)
    vendor_vintf_dir = get_vintf_vendor_path(partition_name, vintf_folder_path_list)
    temp_folder = tempfile.mkdtemp()
    output_folder = os.path.join(temp_folder, partition_name)
    host_bin_dir = find_assemble_vintf(aosp_path)
    merge_vintf_artifacts(aosp_vintf_dir, vendor_vintf_dir, host_bin_dir, output_folder)



def inject_sepolicy(source_dir, aosp_path, aosp_version):
    file_context = "file_contexts"
    file_context_path = os.path.join(source_dir, file_context)
    mapping_sha256_file = "plat_sepolicy_and_mapping.sha256"
    fmapping_sha256_path = os.path.join(source_dir, mapping_sha256_file)
    plat_sepolicy= "sepolicy"
    plat_sepolicy_path = os.path.join(source_dir, plat_sepolicy)

    merged_file_list = [file_context_path, fmapping_sha256_path, plat_sepolicy_path]
    for file_path in merged_file_list:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Could not find file: {file_path}")

    aosp_emulator_out = get_aosp_build_out_dir(aosp_path, aosp_version)
    file_context_dst_path_list = [os.path.join(aosp_emulator_out, "system/etc/selinux/plat_file_contexts"),
                             os.path.join(aosp_emulator_out,"vendor/etc/selinux/vendor_file_contexts")
                             ]
    plat_sepolicy_dst_path_list = [os.path.join(aosp_emulator_out, "vendor/etc/selinux/precompiled_sepolicy")]
    mapping_sha256_file_dst_path_list = [os.path.join(aosp_emulator_out, "system/etc/selinux/plat_sepolicy_and_mapping.sha256"),
                                         os.path.join(aosp_emulator_out, "vendor/etc/selinux/precompiled_sepolicy.plat_sepolicy_and_mapping.sha256")
                                         ]
    mapping_dict = {
        "file_context": {"dst": file_context_dst_path_list, "src": file_context_path},
        "plat_sepolicy": {"dst": plat_sepolicy_dst_path_list, "src": plat_sepolicy_path},
        "mapping_sha256_file": {"dst": mapping_sha256_file_dst_path_list, "src": fmapping_sha256_path},
    }

    for key, value in mapping_dict.items():
        src = str(value["src"])
        for dst in value["dst"]:
            try:
                shutil.copy2(src, dst)
                logging.info(f"Successfully injected {key} to: {dst}")
            except Exception as e:
                logging.error(f"Failed to inject {key} to: {dst}|{src}")
                logging.error(e)


def get_latest_sepolicy_mapping(mapping_dir: str):
    """
    Scans the SELinux mapping directory and returns the absolute file path
    and base version string of the highest API level mapping file.

    Filters out '.compat.cil' files to focus purely on base definition maps.

    Args:
        mapping_dir (str): Path to the system/etc/selinux/mapping/ folder.

    Returns:
        Tuple[str, str]: (absolute_file_path, base_version_major_string)
                          e.g., ('/path/to/mapping/33.0.cil', '33')
                          Returns (None, None) if no valid files are discovered.
    """
    # Look for all .cil files in the directory
    search_pattern = os.path.join(mapping_dir, "*.cil")
    all_files = glob.glob(search_pattern)

    latest_version = -1.0
    latest_file_path = None
    latest_major_version = None

    # Pattern to match clean version schemas like '33.0.cil' while ignoring '.compat.cil'
    version_pattern = re.compile(r"^(\d+\.\d+)\.cil$")

    for file_path in all_files:
        file_name = os.path.basename(file_path)
        match = version_pattern.match(file_name)

        if match:
            version_str = match.group(1)
            try:
                version_float = float(version_str)
                if version_float > latest_version:
                    latest_version = version_float
                    latest_file_path = os.path.abspath(file_path)
                    # Extract the major component (e.g., '33.0' -> '33')
                    latest_major_version = version_str.split('.')[0]
            except ValueError:
                # Skip files that don't parse cleanly as floats
                continue

    return latest_file_path, latest_major_version


def get_vendor_policy_files(vendor_partition_path: str):
    """
    Traverses the vendor partition path to locate the 'etc/selinux' subdirectory,
    then retrieves the absolute paths for vendor_sepolicy.cil and plat_pub_versioned.cil.

    Args:
        vendor_partition_path (str): Root directory of the unpacked vendor partition.

    Returns:
        Tuple[str, str]: (vendor_sepolicy_path, plat_pub_versioned_path)
                          Returns None for individual values if not found.
    """
    vendor_cil = None
    vendor_pub = None

    # Standard target relative signature
    target_suffix = os.path.join("etc", "selinux")

    for root, dirs, files in os.walk(vendor_partition_path):
        # Check if we have arrived inside the target configuration folder
        if root.endswith(target_suffix):
            if "vendor_sepolicy.cil" in files:
                vendor_cil = os.path.abspath(os.path.join(root, "vendor_sepolicy.cil"))
            if "plat_pub_versioned.cil" in files:
                vendor_pub = os.path.abspath(os.path.join(root, "plat_pub_versioned.cil"))
            # Found our target folder layout; break early from structural walking
            break
    return vendor_cil, vendor_pub


def get_policy_files(aosp_path, aosp_version):
    aosp_emulator_out_path = get_aosp_build_out_dir(aosp_path, aosp_version)
    plat_cil = os.path.join(aosp_emulator_out_path, "system/etc/selinux/plat_sepolicy.cil")
    mapping_dir = os.path.join(aosp_emulator_out_path, "system/etc/selinux/mapping/")
    plat_mapping, policy_version = get_latest_sepolicy_mapping(mapping_dir)
    return plat_cil, plat_mapping, policy_version

def handle_seplicy_merging(aosp_version, aosp_path, partition_path_system):
    temp_folder = tempfile.mkdtemp()
    out_dir = os.path.join(temp_folder, "semerger")
    os.makedirs(out_dir, exist_ok=True)
    all_files_path = str(os.path.dirname(partition_path_system))
    vendor_partition_path = os.path.join(all_files_path, "vendor/")
    if not os.path.exists(vendor_partition_path):
        raise FileNotFoundError(f"Vendor partition path: {vendor_partition_path} does not exist for semerger")

    plat_cil, plat_mapping, policy_version = get_policy_files(aosp_path, aosp_version)
    vendor_cil, vendor_pub = get_vendor_policy_files(vendor_partition_path)
    secilc_path = find_secil_binary(aosp_path)

    success = merge_sepolicy_pipeline(
        secilc_bin=secilc_path,
        plat_cil=plat_cil,
        plat_mapping=plat_mapping,
        vendor_cil=vendor_cil,
        vendor_pub_versioned=vendor_pub,
        out_dir=out_dir,
        policy_version=policy_version
    )
    if success:
        logging.info(f"Successfully merged sepolicy folder to: {out_dir}")
        inject_sepolicy(out_dir, aosp_path, aosp_version)
    else:
        logging.error(f"Failed to merge sepolicy folder to: {out_dir}|{secilc_path}|{plat_cil}|{plat_mapping}|{vendor_cil}|{vendor_pub}")
    return success, out_dir


def start_semantic_injector(aosp_path: str, aosp_version:str, partition_name: str, vintf_folder_path_list: list, partition_path: str):
    """
    """
    try:
        handle_vintf_merge(aosp_path, aosp_version, partition_name, vintf_folder_path_list)
    except Exception as e:
        logging.error(e)

    if partition_name == "system":
        try:
            handle_seplicy_merging(aosp_version, aosp_path, partition_path)
        except Exception as e:
            logging.error(e)

























