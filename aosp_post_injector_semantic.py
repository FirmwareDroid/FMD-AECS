import logging
import os
import tempfile

from aosp_sepolicy_merger import merge_sepolicy_pipeline
from aosp_vintf_handler import merge_vintf_artifacts
from common import get_aosp_build_out_dir

VINTF_FOLDER_RELATIVE_PATH = "etc/vintf"
ASSEMBLE_VINTF_PATH_LIST = ["out/host/linux-x86/bin/assemble_vintf"]

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


def find_assemble_vintf(asop_path):
    for candidate_path in ASSEMBLE_VINTF_PATH_LIST:
        bin_path = os.path.join(asop_path, candidate_path)
        if os.path.exists(bin_path):
            bin_dir = os.path.dirname(bin_path)
            logging.info(f"Found assemble vintf folder: {bin_dir}")
            return str(bin_dir)
    raise FileNotFoundError(f"Could not find 'assemble_vintf' in expected paths: {ASSEMBLE_VINTF_PATH_LIST}")


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











def handle_seplicy_merging(secilc_path, plat_cil, plat_mapping, vendor_cil, vendor_pub, out_dir, policy_version, partition_name):
    temp_folder = tempfile.mkdtemp()
    output_folder = os.path.join(temp_folder, partition_name)
    os.makedirs(output_folder, exist_ok=True)

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
        logging.info(f"Successfully merged sepolicy folder to: {output_folder}")
    else:
        logging.error(f"Failed to merge sepolicy folder to: {output_folder}|{secilc_path}|{plat_cil}|{plat_mapping}|{vendor_cil}|{vendor_pub}")
    return success, output_folder


def start_semantic_injector(aosp_path: str, aosp_version:str, partition_name: str, vintf_folder_path_list: list):
    """
    """
    try:
        handle_vintf_merge(aosp_path, aosp_version, partition_name, vintf_folder_path_list)
    except Exception as e:
        logging.error(e)

























