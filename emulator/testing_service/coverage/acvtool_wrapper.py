import os
import shutil
import subprocess
import argparse
import logging
import sys
import urllib.request
import zipfile

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
IMAGE_META_PATH = "/android/image_meta.txt"
ACV_DOWNLOAD_PATH = "/root/acvtool/pickles"
ACV_EXTRACTED_PICKLES_PATH = "/root/acvtool/pickles_ext"
ACV_WD_DIR = "/root/acvtool/acvtool_working_dir"
ACV_WD_PICKLES_DIR = "/root/acvtool/acvtool_working_dir/covered_pickles"

def run_acv_command(cmd_args):
    """
    Run an ACVTool command and return output, error, and exit code.
    """
    try:
        logging.info(f"Running command: {' '.join(cmd_args)}")
        result = subprocess.run(cmd_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=300)
        if result.returncode != 0:
            logging.error(f"Command failed: {' '.join(cmd_args)}")
            logging.error(result.stderr)
        else:
            logging.info(result.stdout)
        return result.returncode, result.stdout, result.stderr
    except Exception as e:
        logging.error(f"Exception running command: {e}")
        return 1, '', str(e)

def get_image_meta():
    """
    Read the image name and repository URL from the image_meta.txt file.
    """
    try:
        with open(IMAGE_META_PATH, 'r') as f:
            lines = f.readlines()
            if len(lines) >= 2:
                image_name = lines[0].strip()
                repo_url = lines[1].strip()
                return image_name, repo_url
            else:
                logging.warning(f"Image meta file {IMAGE_META_PATH} does not contain enough information.")
                return "unknown-image", "unknown-repo"
    except FileNotFoundError:
        logging.warning(f"Image meta file {IMAGE_META_PATH} not found.")
        return "unknown-image", "unknown-repo"

def download_and_extract(file_url, download_path, extraction_path):
    logging.info(f"Starting download from: {file_url}, extracting to: {extraction_path}, downloading to: {download_path}")
    try:
        # Download the file
        urllib.request.urlretrieve(file_url, download_path)
        logging.info(f"Download complete! Saved as '{download_path}'.")

        # Create extraction directory if it doesn't exist
        if not os.path.exists(extraction_path):
            os.makedirs(extraction_path)

        logging.info(f"Extracting files to '{extraction_path}'...")
        # Extract the ZIP file
        with zipfile.ZipFile(download_path, 'r') as zip_ref:
            zip_ref.extractall(extraction_path)

        logging.info("Extraction complete!")

    except Exception as e:
        logging.info(f"An error occurred: {e}")


def find_pickle_file(package, wd=None):
    """
    Find the pickle file for a given package in the extracted pickles directory.
    """
    if wd is None:
        wd = ACV_EXTRACTED_PICKLES_PATH
    for root, dirs, files in os.walk(wd):
        for file in files:
            if file.endswith('.pickle') and package in file:
                return os.path.join(root, file)
    return None

def copy_pickle_to_wd(source_pickle: str, target_wd: str):
    if not os.path.exists(target_wd):
        raise FileNotFoundError(f"Target directory {target_wd} not found.")
    try:
        target_file = str(os.path.join(target_wd, os.path.basename(source_pickle)))
        shutil.copy2(source_pickle, target_file)
    except Exception as e:
        logging.error(f"An error occurred: {e}")
    return

def activate_app(package):
    return run_acv_command(['acv', 'activate', package])

def clear_wd_pickle():
    try:
        shutil.rmtree(ACV_WD_PICKLES_DIR)
        os.makedirs(ACV_WD_PICKLES_DIR)
    except Exception as e:
        logging.error(f"An error occurred: {e}")

def activate_start(package):
    clear_wd_pickle()
    source_pickle = str(find_pickle_file(package, wd=ACV_EXTRACTED_PICKLES_PATH))
    copy_pickle_to_wd(source_pickle, ACV_WD_PICKLES_DIR)
    return run_acv_command(['acv', 'start', package])

def snap_coverage(package, wd=None):
    cmd = ['acv', 'snap', package]
    if wd:
        cmd += ['--wd', wd]
    return run_acv_command(cmd)


def cover_pickles(package, wd=None):
    cmd = ['acv', 'cover-pickles', package]
    if wd:
        cmd += ['--wd', wd]
    return run_acv_command(cmd)


def generate_report(package, wd=None):
    cmd = ['acv', 'report', package]
    if wd:
        cmd += ['--wd', wd]
    return run_acv_command(cmd)


def flush_coverage(package, wd=None):
    cmd = ['acv', 'flush', package]
    if wd:
        cmd += ['--wd', wd]
    return run_acv_command(cmd)


def main():
    parser = argparse.ArgumentParser(description='ACVTool Wrapper CLI')
    subparsers = parser.add_subparsers(dest='command', required=True)

    parser_activate = subparsers.add_parser('activate', help='Activate app')
    parser_activate.add_argument('package', help='App package name')

    parser_snap = subparsers.add_parser('snap', help='Take coverage snapshot')
    parser_snap.add_argument('package', help='App package name')
    parser_snap.add_argument('--wd', required=False, help='Working directory (optional)')

    parser_flush = subparsers.add_parser('flush', help='Reset instruction tracking for an app')
    parser_flush.add_argument('package', help='App package name')
    parser_flush.add_argument('--wd', required=False, help='Working directory (optional)')

    parser_cover = subparsers.add_parser('cover-pickles', help='Apply coverage data to Smali code tree')
    parser_cover.add_argument('package', help='App package name')
    parser_cover.add_argument('--wd', required=False, help='Working directory (optional)')

    parser_report = subparsers.add_parser('report', help='Generate coverage report')
    parser_report.add_argument('package', help='App package name')
    parser_report.add_argument('--wd', required=False, help='Working directory (optional)')

    parser_snap = subparsers.add_parser('start', help='Start coverage tracking for an app')
    parser_snap.add_argument('package', help='App package name')

    args = parser.parse_args()

    if args.command == 'activate':
        activate_app(args.package)
    elif args.command == 'snap':
        snap_coverage(args.package, args.wd)
    elif args.command == 'flush':
        flush_coverage(args.package, args.wd)
    elif args.command == 'cover-pickles':
        cover_pickles(args.package, args.wd)
    elif args.command == 'report':
        generate_report(args.package, args.wd)
    elif args.command == 'start':
        activate_start(args.package)
    elif args.command == 'download':
        image_name, repo_url = get_image_meta()
        file_url = f"{repo_url}/repository/raw_files/acvtool_{image_name}.zip"
        download_and_extract(file_url, f"{ACV_DOWNLOAD_PATH}/acvtool_{image_name}.zip", ACV_EXTRACTED_PICKLES_PATH)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
