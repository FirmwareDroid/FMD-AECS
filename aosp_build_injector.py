"""
A command-line tool that downloads files related to the build process of an Android firmware image and stores them
on disk. Directly extract the downloaded zip content.
"""
import os
import argparse
import shlex
import zipfile
from tqdm import tqdm
import shutil
import subprocess
import glob
import docker
from string import Template
from jinja2 import Environment, FileSystemLoader
from getpass import getpass

from config import AOSP_BUILD_OUT_PATH, AOSP_EMU_ZIP_FILENAME, IMAGE_ARTEFACTS_ABS_PATH, META_BUILD_FILENAME, \
    TEMPLATE_FOLDER, BASE_SYSTEM_FILE_NAME, BASE_PATH, BUILD_OUT_PATH, AECS_ROOT_DIR, EMULATOR_DOCKERFILE_ABS_PATH, \
    DOCKER_PLATFORM, AOSP_PACKAGES_APPS_PATH
from fmd_backend_requests import get_android_app_ids, download_firmware_build_files, get_csrf_token, authenticate_fmd, \
    get_firmware_ids, get_graphql_url


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
    Args:
        dir_path: str - path of the directory to delete files from.

    """
    files = glob.glob(dir_path)
    for f in files:
        os.remove(f)


def start_aosp_build(aosp_path, aosp_packages_path):
    """
    Wrapper method to start the firmware injection and build process.
    Args:
        aosp_packages_path: str - path to the prebuilt package folder of aosp.
        aosp_path: str -  path to aosp root folder.
    """
    inject_packages(aosp_path, aosp_packages_path)
    is_build_success = execute_build_command(aosp_path)
    if is_build_success:
        extract_emulator_image(aosp_path)


def extract_emulator_image(aosp_path):
    """
    Extracts the aosp emulator images to the image artefacts folder for further usage.
    """
    image_source_path = os.path.join(aosp_path, AOSP_BUILD_OUT_PATH, AOSP_EMU_ZIP_FILENAME)
    print(f"Extract image_source_path: {image_source_path} to {IMAGE_ARTEFACTS_ABS_PATH}")
    if os.path.exists(image_source_path):
        #shutil.copy(image_source_path, IMAGE_ARTEFACTS_ABS_PATH)
        extract_zip(image_source_path, IMAGE_ARTEFACTS_ABS_PATH)
    else:
        raise RuntimeError(f"Could not find image zip file: {image_source_path}")


def inject_packages(aosp_path, aosp_packages_path, exclude_list=[]):
    """
    Replaces the original base_system.mk of the AOSP source code with a modified version.
    The modified version includes all the packages to inject into the build process.

    Args:
        exclude_list: list(str) - contains the packages to exclude from the injection.
        aosp_packages_path: str - path to the prebuilt package folder of aosp.
        aosp_path: str -  path to aosp root folder.
    """
    meta_build_path = os.path.join(aosp_path, aosp_packages_path, META_BUILD_FILENAME)
    print(meta_build_path)
    if not os.path.exists(meta_build_path):
        raise RuntimeError(f"Could not find file: {META_BUILD_FILENAME} from {meta_build_path}")
    with open(meta_build_path, 'r') as meta_build_file:
        system_package_name_list = meta_build_file.readlines()
        environment = Environment(loader=FileSystemLoader(TEMPLATE_FOLDER))
        template = environment.get_template(BASE_SYSTEM_FILE_NAME)
        content = template.render(
            system_package_name_list=system_package_name_list
        )

    aosp_base_system_path = os.path.join(aosp_path, BASE_PATH, BASE_SYSTEM_FILE_NAME)
    if os.path.exists(aosp_base_system_path):
        out_file_path = os.path.join(BUILD_OUT_PATH, BASE_SYSTEM_FILE_NAME)
        with open(out_file_path, mode="w", encoding="utf-8") as out_file:
            out_file.write(content)
        shutil.copyfile(out_file_path, aosp_base_system_path)
    else:
        raise RuntimeError(f"AOSP build file does not exist: {aosp_base_system_path}")


def execute_build_command(aosp_path):
    """
    Start the aosp build process.
    Pack all Android images with ("m emu_img_zip"). Copy the artefacts to the local image folder. Unzips the artefacts.
    # https://source.android.com/docs/setup/create/avd#sharing_avd_system_images_for_others_to_use_with_android_studio
    """
    is_build_success = False
    current_directory = os.path.dirname(os.path.realpath(__file__))
    os.chdir(aosp_path)
    aosp_root = shlex.quote(aosp_path)
    command = f"bash -c 'source {aosp_root}/build/envsetup.sh && lunch sdk_x86_64-userdebug && m && m emu_img_zip'"
    try:
        subprocess.run(command, capture_output=True, shell=True, check=True)
        is_build_success = True
    except subprocess.CalledProcessError as err:
        print(f"Got an error building firmware: {err}")
    os.chdir(current_directory)
    return is_build_success


def handle_docker_images(docker_repository_url, firmware_id, docker_user, docker_password):
    """
    Wrapper script to create and push docker container images of the build process.
    Returns:

    """
    authenticate_docker_registry(docker_repository_url, docker_user, docker_password)
    image = build_container_image(firmware_id)
    if image:
        docker_repo_url_without_schema = docker_repository_url.replace("http://", "").replace("https://", "")
        push_container_image(docker_repo_url_without_schema, firmware_id)
    else:
        raise RuntimeError(f"Could not build docker image for firmware {firmware_id}")


def build_container_image(tag):
    """
    Builds a docker container image that includes the image files from the image_artefacts directory.
    """
    docker_client = docker.from_env()
    image = docker_client.images.build(path=AECS_ROOT_DIR,
                                       tag=tag,
                                       dockerfile=EMULATOR_DOCKERFILE_ABS_PATH,
                                       platform=DOCKER_PLATFORM)
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


def clear_environment(aosp_packages_path):
    """
    Reverts the build environment
    Returns:

    """
    # Delete app packages
    package_file_paths = aosp_packages_path + "/ib_*"
    delete_files(package_file_paths)
    os.remove(aosp_packages_path + "/meta_build.txt")
    os.remove(aosp_packages_path + "/apk_meta.txt")

    # Delete image artefacts
    current_directory = os.path.dirname(os.path.realpath(__file__))
    if os.path.exists(IMAGE_ARTEFACTS_ABS_PATH):
        delete_files(IMAGE_ARTEFACTS_ABS_PATH)

    # Revert aosp template files
    aosp_product_path = os.path.join(aosp_root, "/build/make/target/product/")
    for template_path in template_path_list:
        shutil.copy(template_path, aosp_product_path)


def fetch_build_files(firmware_id, graphql_url, cookies, fmd_url, aosp_packages_path):
    """
    Main wrapper routine to download and extract firmware build files for aosp.
    Args:
        firmware_id: str - id of the firmware packages to fetch.
        graphql_url: str - url to the fmd api.
        cookies: cookie jar for requests.
        fmd_url: str - url to the main fmd backend
        aosp_packages_path: str - path to extract the data to.

    """
    print(f"Process firmware: {firmware_id}")
    android_app_id_list = get_android_app_ids(graphql_url, firmware_id, cookies)
    print(f"Fetched Android ids: {len(android_app_id_list)}")
    zip_file_path = download_firmware_build_files(fmd_url,
                                                  android_app_id_list,
                                                  cookies,
                                                  aosp_packages_path)
    extract_zip(zip_file_path, aosp_packages_path)
    os.remove(zip_file_path)
    print(f"\nCompleted firmware build file download to {aosp_packages_path}")


def start_injection():
    fmd_password = getpass()
    graphql_url = get_graphql_url(args.fmd_url)
    csrf_cookie = get_csrf_token(args.fmd_url)
    cookies = authenticate_fmd(graphql_url, args.fmd_username, fmd_password, csrf_cookie)

    firmware_id_list = get_firmware_ids(graphql_url, cookies)
    print(f"Got {len(firmware_id_list)} firmware ids to process...")

    temp_obj = Template(AOSP_PACKAGES_APPS_PATH)
    aosp_packages_path = temp_obj.substitute(aosp_path=args.aosp_path)

    for firmware_id in tqdm(firmware_id_list):
        fetch_build_files(firmware_id, graphql_url, cookies, args.fmd_url, aosp_packages_path)
        start_aosp_build(args.aosp_path, aosp_packages_path)
        handle_docker_images(args.docker_repo_url, firmware_id, args.docker_repo_username, args.docker_repo_password)
        # clear_environment(aosp_packages_path)


def main():
    parser = argparse.ArgumentParser(prog='fmd_build_injector',
                                     description="A cli tool to download and store build files from FirmwareDroid.")

    parser.add_argument("-s", "--aosp-path",
                        type=str,
                        default="/home/ubuntu/aosp_12/",
                        help="Specifies the path to the root of the aosp source code.")
    parser.add_argument("-f", "--fmd-url",
                        type=str,
                        default=None,
                        required=True,
                        help="HTTP/HTTPS url to the FMD instance to grab the packages."
                             "Example: https://firmwaredroid.cloudlab.zhaw.ch")
    parser.add_argument("-u", "--fmd-username",
                        type=str,
                        default=None,
                        required=True,
                        help="Username for the authentication to the fmd service.")
    parser.add_argument("-d", "--docker-repo-username",
                        type=str,
                        default=None,
                        required=True,
                        help="Username for the authentication to the docker registry.")
    parser.add_argument("-p", "--docker-repo-password",
                        type=str,
                        default=None,
                        required=True,
                        help="Password for the authentication to the docker registry.")
    parser.add_argument("-r", "--docker-repo-url",
                        type=str,
                        default=None,
                        help="Specifies the url to a docker registry, where the emulator images will be pushed to.")
    args = parser.parse_args()

    if not (args.fmd_url.startswith("https://") or args.fmd_url.startswith("http://")):
        print(f"Error: Incorrect FMD URL: {args.fmd_url}")
        exit(1)

    start_aosp_build(args.aosp_path, AOSP_PACKAGES_APPS_PATH)




if __name__ == "__main__":
    main()
