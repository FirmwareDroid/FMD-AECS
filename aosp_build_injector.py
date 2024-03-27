"""
A command-line tool that downloads files related to the build process of an Android firmware image and stores them
on disk. Directly extract the downloaded zip content.
"""
import json
import os
import argparse
import shlex
import zipfile
import logging
import shutil
import subprocess
import glob
import docker
import sys
from tqdm import tqdm
from jinja2 import Environment, FileSystemLoader
from getpass import getpass
from config import AOSP_BUILD_OUT_SDK_x86_64_PATH, AOSP_EMU_ZIP_FILENAME, IMAGE_ARTEFACTS_ABS_PATH, \
    TEMPLATE_FOLDER, BASE_SYSTEM_FILE_NAME, BASE_PATH, BUILD_OUT_PATH, ROOT_PATH, EMULATOR_DOCKERFILE_X8664_ABS_PATH, \
    DOCKER_PLATFORM_X86_64, AOSP_PACKAGES_APPS_PATH, DOCKER_PLATFORM_ARM64, SUPPORTED_ARCHITECTURES, \
    SUPPORTED_LUNCH_TARGETS, AOSP_BUILD_OUT_SDK_ARM64_PATH, META_BUILD_FILENAMES, BASE_PRODUCT_FILE_NAME, \
    BASE_VENDOR_FILE_NAME, BASE_FILENAMES, META_BUILD_SYSTEM_FILENAME, EMULATOR_DOCKERFILE_ARM64_ABS_PATH, \
    IMAGE_ARTEFACTS_X86_64_ABS_PATH, IMAGE_ARTEFACTS_ARM64_PATH, FILTERED_APK_FILES, IMAGE_ARTEFACTS_PATH
from fmd_backend_requests import download_firmware_build_files, get_csrf_token, authenticate_fmd, \
    get_firmware_ids, get_graphql_url, upload_image_as_raw

root = logging.getLogger()
root.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
root.addHandler(handler)


def extract_zip(file_path, destination):
    with zipfile.ZipFile(file_path, 'r') as zip_ref:
        zip_ref.extractall(destination)


def authenticate_docker_registry(repo_url, docker_user, docker_password):
    """
    Authenticates to the docker registry via the docker login command.
    Note: For Sonatype Nexus repositories the "Docker Bearer Token" realm must be enabled in the security settings.
    """
    docker_password = shlex.quote(docker_password)
    docker_user = shlex.quote(docker_user)
    repo_url = shlex.quote(repo_url)
    command = f"docker login -p {docker_password} -u {docker_user} {repo_url}"
    subprocess.run(command, capture_output=True, shell=True, check=True)


def delete_files(dir_path):
    """
    Deletes all files in the given directory.

    :param dir_path: str - path of the directory to delete files from.

    """
    files = glob.glob(dir_path)
    for f in files:
        os.remove(f)


def start_aosp_build(aosp_path, aosp_packages_path, firmware_id, lunch_target):
    """
    Wrapper method to start the firmware injection and build process.

    :param lunch_target: str - aosp build argument to select the build arch.
    :param firmware_id: str - object-id of the firmware
    :param aosp_packages_path: str - path to the prebuilt package folder of aosp.
    :param aosp_path: str - path to aosp root folder.

    :returns: bool - True if the build process was successful.

    """
    is_successful = False
    logging.info(f"Start aosp build injection with firmware: {firmware_id}")
    overwrite_partition_size(aosp_path, aosp_packages_path)
    inject_packages(aosp_path, aosp_packages_path)
    retry_attempts = 5
    while not is_successful and retry_attempts > 0:
        try:
            execute_build_command(aosp_path, firmware_id, lunch_target)
            is_successful = True
        except Exception as err:
            logging.error(err)
            retry_attempts -= 1
    return is_successful


def get_emulator_image_path(aosp_path, lunch_target):
    """
    Returns the path to the emulator image zip file based on the lunch target.

    :param aosp_path: str - path to the root of the aosp source code.
    :param lunch_target: str - aosp build argument to select the build arch.

    :returns: str - path to the emulator image zip file.

    """
    if lunch_target == SUPPORTED_LUNCH_TARGETS[0]:
        image_source_path = os.path.join(aosp_path, AOSP_BUILD_OUT_SDK_x86_64_PATH, AOSP_EMU_ZIP_FILENAME)
    elif lunch_target == SUPPORTED_LUNCH_TARGETS[1]:
        image_source_path = os.path.join(aosp_path, AOSP_BUILD_OUT_SDK_ARM64_PATH, AOSP_EMU_ZIP_FILENAME)
    else:
        raise RuntimeError(f"Unsupported build architecture: {lunch_target}")
    return image_source_path


def extract_emulator_image(aosp_path, lunch_target):
    """
    Extracts the aosp emulator images to the image artefacts folder for further usage.
    """
    image_source_path = get_emulator_image_path(aosp_path, lunch_target)
    extract_dir = os.path.join(ROOT_PATH, IMAGE_ARTEFACTS_PATH)

    logging.info(f"Extract image_source_path: {image_source_path} to {extract_dir}")
    if os.path.exists(image_source_path):
        if not os.path.exists(extract_dir):
            os.makedirs(extract_dir)
        extract_zip(image_source_path, extract_dir)
    else:
        raise RuntimeError(f"Could not find image zip file: {image_source_path}")


def get_base_filename(meta_build_filename):
    """
    Returns the base filename of the aosp build file based on the meta_build_filename.

    :param meta_build_filename:

    :returns: str - base filename of the aosp build file.
    """
    if "product" in meta_build_filename:
        return BASE_PRODUCT_FILE_NAME
    elif "vendor" in meta_build_filename:
        return BASE_VENDOR_FILE_NAME
    else:
        return BASE_SYSTEM_FILE_NAME


def read_and_render_template(meta_build_path, base_filename):
    """
    Reads the meta_build.txt file and renders the aosp build file template with the package names.

    :param meta_build_path: str - path to the meta_build.txt file.
    :param base_filename: str - base filename of the aosp build file to use as template.

    :returns: str - rendered aosp build file template.
    """
    with open(meta_build_path, 'r') as meta_build_file:
        system_package_name_list = meta_build_file.readlines()
        template_folder_abs_path = os.path.join(ROOT_PATH, TEMPLATE_FOLDER)
        logging.info(f"Using template folder: {template_folder_abs_path} with base filename: {base_filename}")
        environment = Environment(loader=FileSystemLoader(template_folder_abs_path))
        template = environment.get_template(base_filename)
        return template.render(system_package_name_list=system_package_name_list)


def write_and_copy_file(content, out_file_path, aosp_base_file_path):
    """
    Writes the rendered aosp build file to the out_file_path and copies it to the aosp source code.

    :param content: str - rendered aosp build file template to be written to file.
    :param out_file_path: str - path to write the rendered aosp build file to.
    :param aosp_base_file_path: str - path to the aosp base file to copy the rendered file to.

    """
    with open(out_file_path, mode="w", encoding="utf-8") as out_file:
        out_file.write(content)
    shutil.copyfile(out_file_path, aosp_base_file_path)
    logging.info(f"Placed {os.path.basename(out_file_path)} {aosp_base_file_path} in aosp source")


def get_packages_to_filter(aosp_path):
    """
    Filters the packages based on the filter list.

    :param aosp_path: str - path to the root of the aosp source code.

    :returns: list - list of filtered packages.

    """
    aosp_packages_abs_path = os.path.join(aosp_path, AOSP_PACKAGES_APPS_PATH)
    dirnames_filtered = []
    for dirpath, dirnames, filenames in os.walk(aosp_packages_abs_path):
        for file_name in filenames:
            logging.info(f"Checking file: {file_name} in {dirpath}")
            if file_name in FILTERED_APK_FILES:
                logging.info(f"Found file: {file_name} in {dirpath} to exclude from the build process.")
                dirnames_filtered.append(str(os.path.basename(dirpath)))
    return dirnames_filtered


def filter_packages(meta_build_path, aosp_packages_path):
    """
    Removes the packages based on the filter list from the meta file.

    :param meta_build_path: str - path to the meta_build.txt file.
    :param aosp_packages_path: str - path to the prebuilt package folder of aosp.

    :returns: list - list of filtered packages.

    """
    with open(meta_build_path, 'r') as meta_build_file:
        lines = meta_build_file.readlines()

    package_name_list = get_packages_to_filter(aosp_packages_path)
    if package_name_list and len(package_name_list) > 0:
        logging.info(f"Filtering packages: {package_name_list} from {meta_build_path}")
        lines = [line for line in lines if not any(s in line for s in package_name_list)]

        with open(meta_build_path, 'w') as file:
            file.writelines(lines)


def get_directory_size(directory_path):
    """
    Calculate the size of a directory in bytes.

    :param directory_path: str - path to the directory to calculate the size of.

    :returns: int - size of the directory in bytes.

    """
    total = 0
    for dirpath, dirnames, filenames in os.walk(directory_path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if not os.path.islink(fp):
                total += os.path.getsize(fp)

    return total


def get_minimal_partition_size(aosp_path, aosp_packages_path):
    """
    Calculates the minimal partition size based on the size of the packages to inject.

    :param aosp_path: str - path to the root of the aosp source code.
    :param aosp_packages_path: str - path to the prebuilt package folder of aosp.

    :returns: int - minimal partition size in bytes.

    """
    packages_abs_path = os.path.join(aosp_path, aosp_packages_path)
    total_bytes = get_directory_size(packages_abs_path)
    default_size = 4294967296  # 4GB
    one_gb = 1073741824
    while default_size < total_bytes:
        default_size += one_gb
        logging.info(f"Increasing Default size: {default_size} Total bytes: {total_bytes}")
    return default_size


def overwrite_partition_size(aosp_path, aosp_packages_path):
    """
    Overwrites the partition size in the aosp source code.

    :param aosp_path: str - path to the root of the aosp source code.
    :param aosp_packages_path: str - path to the prebuilt package folder of aosp.

    """
    minimal_partition_size = get_minimal_partition_size(aosp_path, aosp_packages_path)
    super_partition_size = minimal_partition_size + 8388608  # 8MB
    dynamic_partition_size = minimal_partition_size
    board_config_file_path = os.path.join(aosp_path, "build/make/target/board/BoardConfigEmuCommon.mk")
    logging.info(f"Overwriting partition size to: {minimal_partition_size} in {board_config_file_path}")
    with open(board_config_file_path, 'r') as base_file:
        lines = base_file.readlines()
    for i, line in enumerate(lines):
        if "BOARD_SUPER_PARTITION_SIZE" in line:
            lines[i] = f"  BOARD_SUPER_PARTITION_SIZE := {super_partition_size}\n"
        if "BOARD_EMULATOR_DYNAMIC_PARTITIONS_SIZE" in line:
            lines[i] = f"  BOARD_EMULATOR_DYNAMIC_PARTITIONS_SIZE := {dynamic_partition_size}\n"
    with open(board_config_file_path, 'w') as base_file:
        base_file.writelines(lines)


def inject_packages(aosp_path, aosp_packages_path):
    """
    Replaces the original base_system.mk of the AOSP source code with a modified version.
    The modified version includes all the packages to inject into the build process.

    :param aosp_packages_path: str - path to the prebuilt package folder of aosp.
    :param aosp_path: str -  path to aosp root folder.

    """
    for meta_build_filename in META_BUILD_FILENAMES:
        meta_build_path = os.path.join(aosp_path, aosp_packages_path, meta_build_filename)
        if not os.path.exists(meta_build_path):
            if meta_build_filename == META_BUILD_SYSTEM_FILENAME:
                raise RuntimeError(f"Could not find file: {meta_build_filename} from {meta_build_path}")
            else:
                with open(meta_build_path, 'w'):
                    pass
        base_filename = get_base_filename(meta_build_filename)
        filter_packages(meta_build_path, aosp_packages_path)
        content = read_and_render_template(meta_build_path, base_filename)
        aosp_base_file_path = os.path.join(aosp_path, BASE_PATH, base_filename)
        out_file_path = os.path.join(BUILD_OUT_PATH, base_filename)
        write_and_copy_file(content, out_file_path, aosp_base_file_path)
        if not os.path.exists(aosp_base_file_path):
            raise RuntimeError(f"AOSP build file does not exist: {aosp_base_file_path}. Something went wrong injecting "
                               f"the packages into the aosp source code.")


def execute_build_command(aosp_path, firmware_id, lunch_target):
    """
    Start the aosp build process.
    Pack all Android images with ("m emu_img_zip"). Copy the artefacts to the local image folder.

    :param lunch_target: str - aosp build argument to select the build arch.
    :param firmware_id: str - object-id of the firmware
    :param aosp_path: str - path to aosp root folder.

    """
    current_directory = os.path.dirname(os.path.realpath(__file__))
    os.chdir(aosp_path)
    aosp_root = shlex.quote(aosp_path)
    logging.info(f"Starting build process for {lunch_target}... this will take a long time.")

    if lunch_target not in SUPPORTED_LUNCH_TARGETS:
        raise RuntimeError("Unsupported build CPU architecture specified.")

    command = f"bash -c 'source {aosp_root}/build/envsetup.sh " \
              f"&& lunch {lunch_target} " \
              f"&& m sdk -j 80 " \
              f"&& m emu_img_zip'"
    # f"&& m sdk_repo " \
    # f"&& m" \
    try:
        log_name = firmware_id + ".log"
        log_path = os.path.join(BUILD_OUT_PATH, log_name)
        logging.info(f"Build logs will be written to: {log_path}")
        with open(log_path, "w") as outfile:
            subprocess.run(command, shell=True, check=True, stdout=outfile, stderr=outfile)
    except subprocess.CalledProcessError as err:
        logging.error(f"Got an error building firmware: {err}")
        raise err
    os.chdir(current_directory)


def handle_docker_images(docker_repository_url, firmware_id, docker_user, docker_password, docker_build_arch,
                         target_build_arch):
    """
    Wrapper script to create and push docker container images of the build process.
    Returns:

    """
    authenticate_docker_registry(docker_repository_url, docker_user, docker_password)
    image = build_container_image(firmware_id, docker_build_arch, target_build_arch)
    if image:
        docker_repo_url_without_schema = docker_repository_url.replace("http://", "").replace("https://", "")
        push_container_image(docker_repo_url_without_schema, firmware_id)
    else:
        raise RuntimeError(f"Could not build docker image for firmware {firmware_id}")


def build_container_image(tag, docker_build_arch, target_build_arch):
    """
    Builds a docker container image that includes the image files from the image_artefacts directory.
    """
    logging.info(f"Building docker image for firmware: {tag}, arch: {docker_build_arch}, target: {target_build_arch}")
    docker_client = docker.from_env()
    os.chdir(ROOT_PATH)
    if target_build_arch not in SUPPORTED_ARCHITECTURES:
        raise RuntimeError(
            f"Unsupported architecture: {docker_build_arch}. Supported architectures: {SUPPORTED_ARCHITECTURES}")
    if target_build_arch == SUPPORTED_ARCHITECTURES[0]:
        dockerfile_path = EMULATOR_DOCKERFILE_X8664_ABS_PATH
    else:
        dockerfile_path = EMULATOR_DOCKERFILE_ARM64_ABS_PATH
    try:
        log_name = tag + "_docker.log"
        log_path = os.path.join(BUILD_OUT_PATH, log_name)
        logging.info(f"Docker build logs will be written to: {log_path}")
        image, log_generator = docker_client.images.build(path=ROOT_PATH,
                                                          tag=tag,
                                                          dockerfile=dockerfile_path,
                                                          platform=docker_build_arch,
                                                          quiet=False)
        try:
            with open(log_path, "w") as outfile:
                for log in log_generator:
                    dict_string = json.dumps(log)
                    outfile.write(dict_string)
        except Exception as log_error:
            pass
    except Exception as err:
        logging.error(f"Got an error building docker image: {err}")
        raise err
    return image


def push_container_image(docker_repository_url, firmware_id):
    """
    Creates a docker tag and pushes the container image to the docker repository via docker cli.
    """
    firmware_id = shlex.quote(firmware_id)
    docker_repository_url = shlex.quote(docker_repository_url)
    command = f"docker tag {firmware_id}:latest {docker_repository_url}{firmware_id}:latest"
    subprocess.run(command, capture_output=True, shell=True, check=True)

    command = f"docker push {docker_repository_url}{firmware_id}:latest"
    subprocess.run(command, capture_output=True, shell=True, check=True)


def clear_packages(aosp_packages_path):
    """
    Deletes injected apk packages from the aosp source code.

    :param aosp_packages_path:

    """
    logging.info(f"Clearing packages from {aosp_packages_path}")
    try:
        directories = glob.glob(os.path.join(aosp_packages_path, 'ib_*'))
        for directory in directories:
            shutil.rmtree(directory)
    except Exception as err:
        logging.error(err)
    logging.info("Cleared app packages from aosp source code.")


def clear_image_artefacts():
    """
    Deletes the image artefacts.
    """
    logging.info(f"Image artefacts will be deleted from {IMAGE_ARTEFACTS_ABS_PATH}")
    try:
        x86_64_artefact_path = os.path.join(ROOT_PATH, IMAGE_ARTEFACTS_X86_64_ABS_PATH)
        arm64_artefact_path = os.path.join(ROOT_PATH, IMAGE_ARTEFACTS_ARM64_PATH)
        if os.path.exists(x86_64_artefact_path):
            logging.info(f"Clearing image artefacts from {x86_64_artefact_path}")
            shutil.rmtree(x86_64_artefact_path)
        if os.path.exists(arm64_artefact_path):
            logging.info(f"Clearing image artefacts from {arm64_artefact_path}")
            shutil.rmtree(arm64_artefact_path)
            logging.info("Cleared image artefacts from aosp source code.")
    except Exception as err:
        logging.error(err)


def clear_base_files(aosp_path):
    """
    Deletes the base files from the aosp source code.

    :param aosp_path: str - path to the root of the aosp source code.
    """
    try:
        for base_filename in BASE_FILENAMES:
            aosp_base_file_path = os.path.join(aosp_path, BASE_PATH, base_filename)
            if os.path.exists(aosp_base_file_path):
                os.remove(aosp_base_file_path)
                logging.info(f"Removed {aosp_base_file_path} from aosp source code.")
    except Exception as err:
        pass


def clear_environment(aosp_path, aosp_packages_path):
    """
    Reverts the build environment
    Returns:

    """
    clear_packages(aosp_packages_path)
    clear_image_artefacts()
    clear_base_files(aosp_path)


def fetch_build_files(firmware_id, cookies, fmd_url, aosp_packages_abs_path):
    """
    Main wrapper routine to download and extract firmware build files for aosp.
    Args:
        firmware_id: str - id of the firmware packages to fetch.
        cookies: cookie jar for requests.
        fmd_url: str - url to the main fmd backend
        aosp_packages_abs_path: str - path to extract the app packages to.

    """
    logging.info(f"Process firmware: {firmware_id}")
    zip_file_path = download_firmware_build_files(fmd_url,
                                                  firmware_id,
                                                  cookies,
                                                  aosp_packages_abs_path)
    extract_zip(zip_file_path, aosp_packages_abs_path)
    os.remove(zip_file_path)
    logging.info(f"\nCompleted firmware build file download to {aosp_packages_abs_path}")


def parse_arguments():
    """
    Parse the command line arguments.
    """
    parser = argparse.ArgumentParser(prog='fmd_build_injector',
                                     description="A cli tool to download and store build files from FirmwareDroid.")
    parser.add_argument("-s", "--aosp-path", type=str, default="/home/ubuntu/aosp_12/",
                        help="Specifies the path to the root of the aosp source code.")
    parser.add_argument("-f", "--fmd-url", type=str, default=None, required=True,
                        help="HTTP/HTTPS url to the FMD instance to grab the packages."
                             "Example: https://firmwaredroid.cloudlab.zhaw.ch")
    parser.add_argument("-u", "--fmd-username", type=str, default=None, required=True,
                        help="Username for the authentication to the fmd service.")
    parser.add_argument("-d", "--docker-repo-username", type=str, default=None, required=True,
                        help="Username for the authentication to the docker registry.")
    parser.add_argument("-r", "--docker-repo-url", type=str, default=None,
                        help="Specifies the url to a docker registry, where the emulator images will be pushed to.")
    parser.add_argument("-a", "--arch", type=str, default="x86_64",
                        help='Specifies the CPU architecture ("arm64" or "x86_64") to use for the build process.')
    args = parser.parse_args()

    if not (args.fmd_url.startswith("https://") or args.fmd_url.startswith("http://")):
        logging.error(f"Error: Incorrect FMD URL: {args.fmd_url}")
        exit(1)

    return args


def get_passwords(args):
    """
    Get the passwords for the FirmwareDroid and Docker registry.

    :param args:

    :returns: tuple - tuple of the FirmwareDroid and Docker registry passwords.

    """
    fmd_password = os.getenv('FMD_PASSWORD')
    if not fmd_password:
        fmd_password = getpass(f"Please enter your FirmwareDroid password ({args.fmd_username}): ")

    docker_repo_password = os.getenv('DOCKER_REPO_PASSWORD')
    if not docker_repo_password:
        docker_repo_password = getpass(f"Please enter your Docker registry password ({args.docker_repo_username}): ")

    return fmd_password, docker_repo_password


def fetch_firmware_ids(args, fmd_password, csrf_cookie):
    """
    Get the firmware ids from the FirmwareDroid service.

    args: dict - command line arguments.
    fmd_password: str - password for the FirmwareDroid service.
    csrf_cookie: cookie jar for requests.

    :returns: tuple - tuple of the firmware ids and cookies.

    """
    graphql_url = get_graphql_url(args.fmd_url)
    cookies = authenticate_fmd(graphql_url, args.fmd_username, fmd_password, csrf_cookie)
    firmware_id_list = get_firmware_ids(graphql_url, cookies)
    logging.info(f"Got {len(firmware_id_list)} firmware ids to process...")
    return firmware_id_list, cookies


def upload_build_artefact(firmware_id, repo_url, username, password, arch, artefact_path):
    """
    Uploads the build artefact to the docker registry. Retries the upload process if it fails.

    :param firmware_id: str - object-id of the firmware
    :param repo_url: str - URL to the docker registry.
    :param username: str - username for the docker registry.
    :param password: str - password for the docker registry.
    :param arch: str - architecture of the build artefact.
    :param artefact_path: str - path to the build artefact.

    :returns: bool - True if the upload was successful.
    """
    is_upload_success = False
    max_attempts = 5
    while not is_upload_success and max_attempts > 0:
        logging.info(f"Uploading image {firmware_id} to repo. Attempt: {max_attempts}")
        is_upload_success = upload_image_as_raw(repo_url,
                                                firmware_id,
                                                username,
                                                password,
                                                arch,
                                                artefact_path)
        max_attempts -= 1
        if not is_upload_success:
            logging.error(f"Failed to upload image {firmware_id} to repo. Retrying...{max_attempts}")
    return is_upload_success


def process_firmware_ids(args, firmware_id_list, cookies, docker_repo_password):
    aosp_packages_abs_path = os.path.join(args.aosp_path, AOSP_PACKAGES_APPS_PATH)

    if args.arch not in SUPPORTED_ARCHITECTURES:
        raise RuntimeError(f"Unsupported architecture: {args.arch}. Supported architectures: {SUPPORTED_ARCHITECTURES}")

    if args.arch == SUPPORTED_ARCHITECTURES[0]:
        lunch_target = SUPPORTED_LUNCH_TARGETS[0]
        docker_build_arch = DOCKER_PLATFORM_X86_64
    else:
        lunch_target = SUPPORTED_LUNCH_TARGETS[1]
        docker_build_arch = DOCKER_PLATFORM_ARM64

    logging.info(f"Downloading and extracting app packages to: {aosp_packages_abs_path}")
    failed_firmware_ids = []
    clear_environment(args.aosp_path, aosp_packages_abs_path)
    for firmware_id in tqdm(firmware_id_list):
        try:
            logging.info(f"Start fetching for build files for firmware-id: {firmware_id}")
            fetch_build_files(firmware_id, cookies, args.fmd_url, aosp_packages_abs_path)
            logging.info(f"Start emulator image build process for firmware-id: {firmware_id}")
            is_build_success = start_aosp_build(args.aosp_path, AOSP_PACKAGES_APPS_PATH,
                                                firmware_id=firmware_id,
                                                lunch_target=lunch_target)
            if is_build_success:
                logging.info(f"Build process for firmware-id: {firmware_id} was successful.")
                emulator_image_zip_path = get_emulator_image_path(args.aosp_path, lunch_target)
                is_upload_success = upload_build_artefact(firmware_id,
                                                          args.docker_repo_url,
                                                          args.docker_repo_username,
                                                          docker_repo_password,
                                                          args.arch,
                                                          emulator_image_zip_path)
                if is_upload_success:
                    logging.info(f"Upload of firmware-id: {firmware_id} was successful.")
                else:
                    raise RuntimeError(f"Upload process for firmware-id: {firmware_id} failed.")
            else:
                raise RuntimeError(f"Build process for firmware-id: {firmware_id} failed.")
        except Exception as err:
            logging.error(f"Got an error processing firmware-id: {firmware_id}. Error: {err}")
            failed_firmware_ids.append(firmware_id)
        finally:
            clear_environment(args.aosp_path, aosp_packages_abs_path)

    if len(failed_firmware_ids) > 0:
        logging.error(f"Failed to build the following firmware ids: {failed_firmware_ids} for arch: {args.arch}")


def main():
    args = parse_arguments()
    fmd_password, docker_repo_password = get_passwords(args)
    csrf_cookie = get_csrf_token(args.fmd_url)
    firmware_id_list, cookies = fetch_firmware_ids(args, fmd_password, csrf_cookie)
    process_firmware_ids(args, firmware_id_list, cookies, docker_repo_password)


if __name__ == "__main__":
    main()
