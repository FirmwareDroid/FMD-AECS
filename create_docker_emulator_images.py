import argparse
import json
import logging
import os
import shlex
import shutil
import subprocess
from getpass import getpass

import docker
from werkzeug.utils import secure_filename

from common import extract_zip
from config import ROOT_PATH, IMAGE_ARTEFACTS_X86_64_ABS_PATH, IMAGE_ARTEFACTS_ARM64_PATH, IMAGE_ARTEFACTS_ABS_PATH, \
    NEXUS_EMULATOR_REPOSITORY, EMULATOR_IMG_ABS_PATH, SUPPORTED_ARCHITECTURES, EMULATOR_DOCKERFILE_X8664_ABS_PATH, \
    EMULATOR_DOCKERFILE_ARM64_ABS_PATH, BUILD_OUT_PATH, NEXUS_DOCKER_EMULATOR_REPOSITORY, \
    EMULATOR_DOCKERFILE_BASE_ABS_PATH
from fmd_backend_requests import download_file, fetch_emulator_image_list
from setup_logger import setup_logger

setup_logger()


def download_emulator_images(repository_url, image_list):
    """
    Downloads the emulator image from the repository.

    :param repository_url: URL to the repository where the emulator image is stored.
    :param image_list: List of emulator images to download.

    :returns: List of downloaded emulator images.

    """
    destination_files = []
    destination = str(os.path.join(ROOT_PATH, EMULATOR_IMG_ABS_PATH))
    for image in image_list:
        filename = image["id"]
        filename = secure_filename(filename)
        if filename is None:
            raise Exception("Image ID is missing.")
        logging.info(f"Downloading emulator image: {filename}")
        download_url = f"{repository_url}{NEXUS_EMULATOR_REPOSITORY}{filename}"
        logging.info(f"Downloading emulator image from {download_url} to {destination}")
        if not os.path.exists(destination):
            os.makedirs(destination)
        destination_file = os.path.join(destination, filename)
        download_file(download_url, destination_file)
        destination_files.append(destination_file)
    return destination_files


def get_image_file_list_form_disk():
    emulator_images = [os.path.join(EMULATOR_IMG_ABS_PATH, img) for img in os.listdir(EMULATOR_IMG_ABS_PATH)]
    logging.info(f"Emulator images: {len(emulator_images)}: {emulator_images}")
    return emulator_images


def get_emulator_image_list(repository_url):
    """
    Fetches the emulator image list from the repository.
    :param repository_url: URL to the repository where the emulator image is stored.
    """
    logging.info(f"Downloading emulator images from {repository_url}")
    response_json = fetch_emulator_image_list(repository_url)
    result = response_json["result"]
    logging.debug(f"Result: {result}")
    if result["success"] and result["data"] is not None:
        for image_meta in result["data"]:
            logging.debug(f"Emulator image: {image_meta}")
    else:
        raise Exception(f"Failed to fetch emulator image list from {repository_url}")
    return result["data"]


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
    os.system("docker builder prune -f")


def extract_emulator_images_to_image_artefacts(emulator_image_path):
    extract_zip(emulator_image_path, IMAGE_ARTEFACTS_ABS_PATH)
    logging.info(f"Extracted emulator images to {IMAGE_ARTEFACTS_ABS_PATH}")


def authenticate_docker_registry(repo_url, docker_user, docker_password):
    """
    Authenticates to the docker registry via the docker login command.
    Note: For Sonatype Nexus repositories the "Docker Bearer Token" realm must be enabled in the security settings.
    """
    docker_password = shlex.quote(docker_password)
    docker_user = shlex.quote(docker_user)
    repo_url = shlex.quote(repo_url)
    repo_url = f"{repo_url}"
    command = f"echo {docker_password} | docker login --password-stdin -u {docker_user} {repo_url}"
    subprocess.run(command, capture_output=True, shell=True, check=True)
    logging.debug(f"Authenticated to the docker registry: {repo_url}")


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
    for arch in SUPPORTED_ARCHITECTURES:
        try:
            image = client.images.get(f"fmd-emulator_{arch}")
            logging.info(f"Base image {image.id} exists.")
        except Exception as err:
            base_images_exist = False
            logging.error(f"Base image fmd-emulator_{arch} not found.: {err}")
            break
    return base_images_exist


def create_base_images():
    """
    Creates the base images for the emulator.
    Returns:

    """
    for arch in SUPPORTED_ARCHITECTURES:
        logging.info(f"Building base image for {arch}")
        build_container_image(f"fmd-emulator_{arch}", f"linux/{arch}", f"{EMULATOR_DOCKERFILE_BASE_ABS_PATH}{arch}")


def process_images(repository_url, docker_repo_url, repository_username, build_local):
    if not check_if_base_images_exists():
        create_base_images()

    #emulator_image_list = get_emulator_image_list(repository_url)
    emulator_zip_file_list = get_image_file_list_form_disk()
    logging.info(f"Processing images: {len(emulator_zip_file_list)}")
    for emulator_zip_path in emulator_zip_file_list:
        logging.info(f"Processing emulator image: {emulator_zip_path}")
        extract_emulator_images_to_image_artefacts(emulator_zip_path)
        filename = os.path.basename(emulator_zip_path)
        if "arm64" in filename:
            docker_build_arch = "linux/arm64"
        else:
            docker_build_arch = "linux/amd64"
        build_container_image(filename.replace(".zip", ""), docker_build_arch)
        if not build_local:
            repository_password = get_repo_password(repository_username)
            authenticate_docker_registry(docker_repo_url, repository_username, repository_password)
            push_container_image(docker_repo_url, filename.replace(".zip", ""))
        else:
            logging.info("Skipping pushing the image to the docker repository.")
        clear_image_artefacts()
        clear_docker_builder()
    logging.info("Finished processing images.")



def parse_arguments():
    parser = argparse.ArgumentParser(
        prog="create_startup_scripts.py",
        description="Downloads emulator images from the repository and builds docker images.",
        add_help=True)
    parser.add_argument("-l",
                        "--create_local",
                        type=bool,
                        default=False,
                        required=False,
                        help="If set skips the download of the emulator images and uses the local files.")
    parser.add_argument("-r",
                        "--repository-url",
                        type=str,
                        required=False,
                        help="URL to the repository where images will be downloaded.")
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
    return parser.parse_args()


def main():
    args = parse_arguments()
    clear_image_artefacts()

    if not args.create_local:
        if not args.repository_url or not args.docker_repo_url or not args.repository_username:
            raise ValueError("Repository URL, Docker repository URL and repository username must be provided.")
        emulator_image_list = get_emulator_image_list(args.repository_url)
        download_emulator_images(args.repository_url, emulator_image_list)
    else:
        logging.info("Skipping download of emulator images.")
    process_images(args.repository_url, args.docker_repo_url, args.repository_username, args.create_local)


if __name__ == "__main__":
    main()
