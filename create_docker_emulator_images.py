import argparse
import json
import logging
import os
import shlex
import shutil
import subprocess
import time
from getpass import getpass
import docker
from werkzeug.utils import secure_filename
import platform
from common import extract_zip
from fmd_backend_requests import download_file, fetch_emulator_image_list
from setup_logger import setup_logger

setup_logger()

IMAGE_ARTEFACTS_ARM64_PATH = "image_artefacts/arm64-v8a/"
IMAGE_ARTEFACTS_X86_64_PATH = "image_artefacts/x86_64/"
IMAGE_ARTEFACTS_PATH = "image_artefacts/"
ROOT_PATH = os.path.dirname(os.path.realpath(__file__))
IMAGE_ARTEFACTS_X86_64_ABS_PATH = os.path.join(ROOT_PATH, IMAGE_ARTEFACTS_X86_64_PATH)
IMAGE_ARTEFACTS_ABS_PATH = os.path.join(ROOT_PATH, IMAGE_ARTEFACTS_PATH)
EMULATOR_DOCKERFILE_X8664_ABS_PATH = os.path.join(ROOT_PATH, "emulator/Dockerfile_x86_64")
EMULATOR_DOCKERFILE_ARM64_ABS_PATH = os.path.join(ROOT_PATH, "emulator/Dockerfile_arm64")
EMULATOR_DOCKERFILE_BASE_ABS_PATH = os.path.join(ROOT_PATH, "emulator/Dockerfile_base_emulator_")

def download_emulator_images(image_list, destination):
    """
    Downloads the emulator images from the repository to the specified destination.

    :param image_list: List of emulator images to download.
    :param destination: Path where the downloaded files will be stored.

    :returns: List of downloaded emulator images.
    """
    destination_file_list = []
    if not os.path.exists(destination):
        os.makedirs(destination, exist_ok=True)

    for asset_dict in image_list:
        filename = asset_dict['path']
        download_url = asset_dict['downloadUrl']
        logging.info(f"Downloading emulator image: {filename}")
        destination_file = os.path.join(destination, filename)
        logging.info(f"Downloading emulator image from {download_url} to {destination_file}")
        download_file(download_url, destination_file)
        destination_file_list.append(destination_file)

    return destination_file_list


def get_filtered_emulator_image_list(repository_url, file_list):
    """
    Fetches and filters the emulator image list based on the provided file list.

    :param repository_url: URL to the repository where the emulator images are stored.
    :param file_list: List of filenames to download.

    :returns: Filtered list of emulator images.
    """
    logging.info(f"Fetching emulator images from {repository_url}")
    asset_list = fetch_emulator_image_list(repository_url)
    if not asset_list or len(asset_list) == 0:
        raise Exception("Failed to fetch emulator image list")
    if file_list and len(file_list) > 0:
        filtered_list = [asset for asset in asset_list if asset['path'] in file_list]
        logging.info(f"Filtered emulator images: {len(filtered_list)}")
    else:
        filtered_list = asset_list
    return filtered_list


def get_image_file_list_form_disk(local_repo_path):
    if not os.path.exists(local_repo_path):
        os.makedirs(local_repo_path, exist_ok=True)

    if not os.path.exists(local_repo_path):
        raise ValueError(f"Local repository path does not exist: {local_repo_path}")
    if not os.path.isdir(local_repo_path):
        raise ValueError(f"Local repository path is not a directory: {local_repo_path}")

    emulator_images = [os.path.join(local_repo_path, img) for img in os.listdir(local_repo_path)]
    logging.info(f"Emulator images in {local_repo_path}: {len(emulator_images)}: {emulator_images}")
    return emulator_images


def get_emulator_image_list(repository_url):
    """
    Fetches the emulator image list from the repository.
    :param repository_url: URL to the repository where the emulator image is stored.
    """
    logging.info(f"Downloading emulator images from {repository_url}")
    asset_list = fetch_emulator_image_list(repository_url)
    if not asset_list or len(asset_list) == 0:
        raise Exception("Failed to fetch emulator image list")
    logging.info(f"Found emulator images: {len(asset_list)}")
    return asset_list


def clear_image_artefacts():
    """
    Deletes the image artefacts.
    """
    logging.debug(f"Image artefacts will be deleted from {IMAGE_ARTEFACTS_ABS_PATH}")
    try:
        x86_64_artefact_path = os.path.join(ROOT_PATH, IMAGE_ARTEFACTS_X86_64_ABS_PATH)
        arm64_artefact_path = os.path.join(ROOT_PATH, IMAGE_ARTEFACTS_ARM64_PATH)
        if os.path.exists(x86_64_artefact_path):
            logging.debug(f"Clearing image artefacts from {x86_64_artefact_path}")
            shutil.rmtree(x86_64_artefact_path)
        if os.path.exists(arm64_artefact_path):
            logging.debug(f"Clearing image artefacts from {arm64_artefact_path}")
            shutil.rmtree(arm64_artefact_path)
            logging.debug("Cleared image artefacts from aosp source code.")
    except Exception as err:
        logging.error(err)


def clear_docker_builder():
    """
    Clears the docker builder.
    """
    os.system("docker container prune -f")
    os.system("docker builder prune -f")
    os.system("docker image prune -f")


def extract_emulator_images_to_image_artefacts(emulator_image_path):
    extract_zip(emulator_image_path, IMAGE_ARTEFACTS_ABS_PATH)
    logging.info(f"Extracted emulator images to {IMAGE_ARTEFACTS_ABS_PATH}")


def authenticate_docker_registry(repo_url, docker_user, docker_password):
    """
    Authenticates to the docker registry via the docker login command.
    Note:
        For Sonatype Nexus repositories the "Docker Bearer Token" realm must be enabled in the security settings.
        The docker repository has as well it's own port (e.g. 8081).
    """
    docker_password = shlex.quote(docker_password)
    docker_user = shlex.quote(docker_user)
    repo_url = shlex.quote(repo_url)
    repo_url = f"{repo_url}"
    command = f"echo {docker_password} | docker login --password-stdin -u {docker_user} {repo_url}"

    try:
        result = subprocess.run(command, capture_output=True, shell=True, check=True, text=True)
        logging.debug(f"Authenticated to the docker registry: {repo_url}")
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to authenticate to the docker registry: {repo_url}")
        logging.error(f"Command: {e.cmd}")
        logging.error(f"Return Code: {e.returncode}")
        logging.error(f"Error Output: {e.stderr.strip()}")
        raise RuntimeError(f"Authentication to the docker registry failed. See logs for details.")


def build_container_image(tag, build_arch, dockerfile_path=None):
    """
    Builds a docker container image that includes the image files from the image_artefacts directory.
    """
    logging.info(f"Building docker image for firmware: {tag}, arch: {build_arch}")

    os.chdir(ROOT_PATH)
    if not dockerfile_path:
        if "arm64" in build_arch:
            dockerfile_path = EMULATOR_DOCKERFILE_ARM64_ABS_PATH
        else:
            dockerfile_path = EMULATOR_DOCKERFILE_X8664_ABS_PATH

    p = subprocess.run(f"docker build -t {tag} -f {dockerfile_path} --no-cache --platform {build_arch} .",
                       shell=True, check=True)
    return p.returncode == 0


def push_container_image(docker_repository_url, filename):
    """
    Creates a docker tag and pushes the container image to the docker repository via docker cli.
    """
    docker_repository_url = docker_repository_url.replace("http://", "").replace("https://", "")
    docker_repository_url = shlex.quote(docker_repository_url)
    docker_repository_url = f"{docker_repository_url}"

    command = f"docker tag {filename}:latest {docker_repository_url}{filename}:latest"
    logging.info(f"Tagging docker image: {command}")
    subprocess.run(command, capture_output=True, shell=True, check=True)

    command = f"docker push {docker_repository_url}{filename}:latest"
    logging.info(f"Pushing docker image command: {command}")
    subprocess.run(command, capture_output=True, shell=True, check=True)
    logging.info(f"Pushed docker image to the docker repository: {docker_repository_url}")

    command = f"docker rmi {docker_repository_url}{filename}"
    subprocess.run(command, capture_output=True, shell=True, check=True)
    logging.info(f"Removed local docker image: {filename}")


def get_repo_password(repo_username):
    docker_repo_password = os.getenv('DOCKER_REPO_PASSWORD')
    if not docker_repo_password:
        docker_repo_password = getpass(f"Please enter your Docker registry password ({repo_username}): ")
    return docker_repo_password


def validate_urls(repository_url, docker_repo_url):
    if not repository_url.endswith('/'):
        repository_url = f'{repository_url}/'
    if not repository_url.startswith('http://') and not repository_url.startswith('https://'):
        raise ValueError("Repository URL must start with http:// or https://")

    if not docker_repo_url.endswith('/'):
        docker_repo_url = f'{docker_repo_url}/'
    if not docker_repo_url.startswith('http://') and not docker_repo_url.startswith('https://'):
        raise ValueError("Docker repository URL must start with http:// or https://")
    return repository_url, docker_repo_url


def check_if_base_images_exists():
    """
    Checks if the base image exists in the docker registry.

    Returns:

    """
    base_images_exist = True
    client = docker.from_env()
    for arch in ["x86_64", "arm64"]:
        try:
            image = client.images.get(f"fmd-emulator_{arch}")
            logging.info(f"Base image {image.id} exists.")
        except Exception as err:
            base_images_exist = False
            logging.warning(f"Base image fmd-emulator_{arch} not found.: {err}")
            break
    return base_images_exist


def get_host_architecture():
    architecture = platform.machine()
    return architecture


def create_base_images():
    """
    Creates the base images for the emulator.
    Returns:

    """
    for arch in ["x86_64", "arm64"]:
        if arch in get_host_architecture():
            logging.info(f"Building base image for {arch}")
            build_container_image(f"fmd-emulator_{arch}", f"linux/{arch}", f"{EMULATOR_DOCKERFILE_BASE_ABS_PATH}{arch}")


def delete_emulator_images(local_repo_path):
    """
    Deletes the emulator images from the local repository path.
    :param local_repo_path: Path to the local repository where the emulator images are stored.
    """
    if os.path.exists(local_repo_path):
        logging.info(f"Deleting emulator images from {local_repo_path}")
        shutil.rmtree(local_repo_path)
    else:
        logging.warning(f"Local repository path does not exist: {local_repo_path}")


def process_images(input_dir, docker_repo_url, repository_username, build_local):
    if not check_if_base_images_exists():
        create_base_images()
    emulator_zip_file_list = get_image_file_list_form_disk(input_dir)
    logging.info(f"Processing images: {len(emulator_zip_file_list)}")
    for emulator_zip_path in emulator_zip_file_list:
        logging.info(f"Processing emulator image: {emulator_zip_path}")
        extract_emulator_images_to_image_artefacts(emulator_zip_path)
        filename = os.path.basename(emulator_zip_path)
        if "arm64" in filename:
            docker_build_arch = "linux/arm64"
        elif "x86_64" in filename:
            docker_build_arch = "linux/amd64"
        else:
            logging.error(f"Unsupported architecture in filename: {filename}. Skipping.")
            continue
        logging.info(f"Building emulator image: {filename} for architecture: {docker_build_arch}")
        build_container_image(filename.replace(".zip", ""), docker_build_arch)
        if not build_local:
            repository_password = get_repo_password(repository_username)
            authenticate_docker_registry(docker_repo_url, repository_username, repository_password)
            push_container_image(docker_repo_url, filename.replace(".zip", ""))
        else:
            logging.info("Skipped pushing the image to the docker repository. Only local build.")
        clear_image_artefacts()
        clear_docker_builder()
    logging.info("Finished processing images.")


def clear_environment(local_repo_path):
    clear_image_artefacts()
    clear_docker_builder()
    delete_emulator_images(local_repo_path)



def parse_arguments():
    parser = argparse.ArgumentParser(
        prog="create_startup_scripts.py",
        description="Downloads emulator images from the repository and builds docker images. Examples:"
                    "\nBuild emulator images from local files: python create_docker_emulator_images.py -l -i ./emulator_images",
        add_help=True)
    parser.add_argument("-l",
                        "--create_local",
                        action='store_true',
                        default=False,
                        required=False,
                        help="If set, skips the download of the emulator images and uses the local files from the input directory.")
    parser.add_argument("-r",
                        "--repository-url",
                        type=str,
                        required=False,
                        help="URL to the nexus repository REST service where the meta-data and images will be downloaded from. Example: https://fmd-repo.cloudlab.zhaw.ch:8443/service/rest/v1/assets?repository=emulator-images")
    parser.add_argument("-d",
                        "--docker-repo-url",
                        type=str,
                        required=False,
                        help="URL to the docker registry where images will be uploaded.")
    parser.add_argument("-u",
                        "--repository-username",
                        type=str,
                        default=None,
                        required=False,
                        help="Username for the authentication to the docker registry.")
    parser.add_argument("-i",
                        "--input-dir",
                        type=str,
                        required=False,
                        default="./emulator_images",
                        help="Path where the output files will be stored.")
    parser.add_argument("--file-list",
                        type=str,
                        required=False,
                        help="Comma-separated list of filenames to download from the repository.")
    return parser.parse_args()


def main():
    args = parse_arguments()

    if not args.create_local:
        clear_environment(args.input_dir)
        if not args.repository_url or not args.docker_repo_url or not args.repository_username:
            raise ValueError("Repository URL, Docker repository URL and repository username must be provided.")
        if not args.input_dir:
            raise ValueError("Download destination must be provided.")
        if not args.file_list:
            file_list = []
        file_list = args.file_list.split(",")
        filtered_image_list = get_filtered_emulator_image_list(args.repository_url, file_list)
        failed_images = []
        successful_images = []
        for image in filtered_image_list:
            try:
                image_list = [image]
                download_emulator_images(image_list, args.input_dir)
                start_time = time.time()
                process_images(args.input_dir, args.docker_repo_url, args.repository_username,
                               args.create_local)
                end_time = time.time()
                elapsed_time_seconds = end_time - start_time
                elapsed_time_minutes = elapsed_time_seconds / 60
                with open("results.log", "a") as log_file:
                    log_file.write(
                        f"Processing images took {elapsed_time_seconds:.2f} seconds ({elapsed_time_minutes:.2f} minutes).\n")
                successful_images.append(image)
                logging.info(f"Successfully processed image: {image}")
            except Exception as e:
                logging.error(f"Error processing image {image['path']}: {e}")
                failed_images.append(image['path'])
            logging.info(f"Number of images done: {len(successful_images) + len(failed_images)} out of {len(filtered_image_list)}")
        logging.info(
            f"Finished processing images. Successful images: {successful_images}. Failed images: {failed_images}.")
    else:
        logging.info("Skipping download of emulator images.")
        process_images(args.input_dir, args.docker_repo_url, args.repository_username, args.create_local)


if __name__ == "__main__":
    main()
