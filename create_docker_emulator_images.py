import argparse
import json
import logging
import os
import shlex
import shutil
import subprocess
import time
from getpass import getpass
import platform
import job_queue
import csv
try:
    import docker
except Exception as _e:
    # docker SDK may not be available or may fail to initialize on some platforms.
    # We'll fall back to using the docker CLI where appropriate.
    docker = None
from common import extract_zip
from fmd_backend_requests import download_file, fetch_emulator_image_list
from setup_logger import setup_logger
from concurrent.futures import ThreadPoolExecutor, as_completed
import multiprocessing
import threading
import datetime

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


def process_single_image(task_tuple):
    """Top-level worker to process a single emulator zip.

    task_tuple: (emulator_zip_path, docker_repo_url, repository_username, repository_password, build_local, results_dir[, delete_zip])
    Returns a result dict.
    """
    if len(task_tuple) == 7:
        emulator_zip_path, docker_repo_url, repository_username, repository_password, build_local, results_dir, delete_zip = task_tuple
    else:
        emulator_zip_path, docker_repo_url, repository_username, repository_password, build_local, results_dir = task_tuple
        delete_zip = False
    filename = os.path.basename(emulator_zip_path)
    tag = filename.replace('.zip', '')
    result = {
        'image': filename,
        'tag': tag,
        'start_time': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'success': False,
        'error': None,
    }
    extracted_dir = None
    try:
        logging.info(f"[worker] Processing emulator image: {emulator_zip_path}")
        # Create a per-image extraction dir
        name = os.path.splitext(os.path.basename(emulator_zip_path))[0]
        extracted_dir = os.path.join(IMAGE_ARTEFACTS_ABS_PATH, name)
        os.makedirs(extracted_dir, exist_ok=True)
        logging.info(f"[worker] Created extracted dir: {extracted_dir}")
        extract_zip(emulator_zip_path, extracted_dir)
        if delete_zip and emulator_zip_path and os.path.exists(emulator_zip_path):
            try:
                os.remove(emulator_zip_path)
                logging.info(f"[worker] Deleted downloaded zip archive after extraction: {emulator_zip_path}")
            except Exception as rm_err:
                logging.warning(f"[worker] Failed to delete zip archive {emulator_zip_path}: {rm_err}")
        if not os.path.isdir(extracted_dir) and not os.path.exists(extracted_dir):
            raise RuntimeError(f"Directory {extracted_dir} does not exist")

        # determine docker arch from filename
        if 'arm64' in filename:
            docker_build_arch = 'linux/arm64'
        elif 'x86_64' in filename:
            docker_build_arch = 'linux/amd64'
        else:
            raise RuntimeError(f"Unsupported architecture in filename: {filename}")

        image_artefact_path = os.path.join(f"./{IMAGE_ARTEFACTS_PATH}", name)
        logging.info(f"[worker] Building emulator image: {filename} for architecture: {docker_build_arch}")
        build_ok = build_container_image(tag, docker_build_arch, extracted_image_dir=image_artefact_path, docker_repo_url=docker_repo_url)

        if not build_ok:
            raise RuntimeError(f"Docker build failed for {tag}")

        if not build_local:
            # Defer pushing to the main process to allow controlled parallel pushes
            logging.info(f"[worker] Build complete for {tag}; push will be performed by the main process.")
        else:
            logging.info(f"[worker] Skipped pushing the image {tag} to the docker repository. Only local build.")

        result['success'] = True
    except Exception as e:
        logging.error(f"[worker] Error processing {emulator_zip_path}: {e}")
        result['error'] = str(e)
    finally:
        result['end_time'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        # write per-image result
        out_file = os.path.join(results_dir, f"{tag}.json")
        try:
            with open(out_file, 'w', encoding='utf-8') as of:
                json.dump(result, of, indent=2)
        except Exception:
            logging.exception(f"Failed to write result for {tag} to {out_file}")
        # Ensure zip file is deleted if requested and still present
        try:
            if delete_zip and emulator_zip_path and os.path.exists(emulator_zip_path):
                logging.info(f"Deleting zip file {emulator_zip_path}")
                os.remove(emulator_zip_path)
        except Exception:
            logging.debug(f"[worker] Failed to remove zip file {emulator_zip_path}; continuing")
        # cleanup extracted artifacts for this image
        try:
            if extracted_dir and os.path.exists(extracted_dir):
                logging.info(f"Deleting directory {extracted_dir}")
                shutil.rmtree(extracted_dir)
        except Exception:
            logging.debug(f"[worker] Failed to remove extracted dir {extracted_dir}; continuing")
        return result


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


def build_container_image(tag, build_arch, dockerfile_path=None, extracted_image_dir=None, docker_repo_url=None):
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
    if extracted_image_dir:
        logging.info(f"Extracting docker image from {extracted_image_dir}. Repo URL: {docker_repo_url}, Tag: {tag}")
        cmd = [
            "docker", "build",
            "--build-arg", f"REPO_URL={docker_repo_url}",
            "--build-arg", f"IMAGE_NAME={tag}",
            "--build-arg", f"IMAGE_ARTEFACTS_SRC={extracted_image_dir}",
            "-t", tag,
            "-f", dockerfile_path,
            "--no-cache",
            "--platform", build_arch,
            "."
        ]
        p = subprocess.run(cmd, check=True)
        #p = subprocess.run(f"docker build --build-arg REPO_URL='{docker_repo_url}' --build-arg IMAGE_NAME='{tag}' --build-arg IMAGE_ARTEFACTS_SRC={extracted_image_dir} -t {tag} -f {dockerfile_path} --no-cache --platform {build_arch} .",
        #                   shell=True, check=True)
    else:
        #p = subprocess.run(f"docker build -t {tag} -f {dockerfile_path} --no-cache --platform {build_arch} .",
        #                   shell=True, check=True)
        cmd = [
            "docker", "build",
            "--build-arg", f"REPO_URL={docker_repo_url}",
            "--build-arg", f"IMAGE_NAME={tag}",
            "-t", tag,
            "-f", dockerfile_path,
            "--no-cache",
            "--platform", build_arch,
            "."
        ]
        p = subprocess.run(cmd, check=True)
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
    Check if the base image for the current host architecture exists in the local Docker.
    Returns True if the image fmd-emulator_<arch> exists for the detected arch, False otherwise.
    """
    mach = (platform.machine() or '').lower()

    # Normalize common machine names to our image suffixes
    if mach in ('x86_64', 'amd64'):
        arch = 'x86_64'
    elif mach in ('arm64', 'aarch64'):
        arch = 'arm64'
    else:
        arch = mach or 'unknown'

    system_name = (platform.system() or '').lower()

    # Try docker SDK on Linux first. On macOS/Darwin we will use the docker CLI only.
    logging.debug(f"Docker host OS detected: {system_name}. docker SDK available: {docker is not None}")
    if system_name == 'linux' and docker is not None:
        logging.info("Attempting to check base image using docker Python SDK (linux host)")
        try:
            if docker is None:
                raise RuntimeError("docker python SDK not available")
            client = docker.from_env()
            try:
                client.images.get(f"fmd-emulator_{arch}")
                logging.info(f"Base image fmd-emulator_{arch} exists (checked via docker SDK)")
                return True
            except Exception as err:
                # If the docker SDK is present and the exception type indicates ImageNotFound,
                # treat it as 'not found'. Otherwise, log and fall back to CLI.
                try:
                    img_not_found = docker.errors.ImageNotFound if docker is not None else None
                except Exception:
                    img_not_found = None

                if img_not_found is not None and isinstance(err, img_not_found):
                    logging.info(f"Base image fmd-emulator_{arch} not found (docker SDK)")
                    return False
                logging.warning(f"docker SDK check failed, falling back to CLI: {err}")
        except Exception as err:
            logging.debug(f"docker.from_env() failed or unavailable: {err}")

    # Fallback to docker CLI (used on darwin/macOS or when SDK is unavailable)
    logging.info("Falling back to docker CLI to check base image")
    docker_cli = shutil.which('docker')
    if not docker_cli:
        logging.warning('docker CLI not found in PATH; cannot check for base image')
        return False

    try:
        cmd = [docker_cli, 'images', '-q', f'fmd-emulator_{arch}']
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode == 0 and (res.stdout or '').strip():
            img_id = res.stdout.strip().splitlines()[0].strip()
            logging.info(f"Base image {img_id} exists for arch={arch}.")
            return True
        else:
            logging.info(f"Base image fmd-emulator_{arch} not found (docker images returned empty)")
            return False
    except Exception as err:
        logging.warning(f"Failed to query docker images for base image: {err}")
        return False


def get_host_architecture() -> str:
    arch = (platform.machine() or "").lower()
    if arch in ("x86_64", "amd64", "x64", "i386", "i686"):
        return "x86_64"
    if arch in ("aarch64", "arm64", "armv8l", "armv8", "arm64e"):
        return "arm64"
    return arch  # fallback: return raw string for logging / future checks


def _is_docker_available() -> bool:
    """Return True if the docker CLI or daemon appears reachable.

    Prefer `docker info` via subprocess because docker-py may surface low-level
    socket errors that are harder to interpret. This works on macOS/Linux.
    """
    # First try docker CLI (works on macOS and Linux with Docker Desktop)
    try:
        docker_bin = shutil.which('docker') or 'docker'
        res = subprocess.run([docker_bin, 'info'], capture_output=True, text=True, timeout=8)
        if res.returncode == 0:
            return True
    except Exception:
        pass

    # On Linux try docker SDK as a fallback (it may connect to daemon directly).
    system_name = (platform.system() or '').lower()
    if system_name == 'linux' and docker is not None:
        logging.info('Docker CLI not reachable; attempting docker Python SDK on linux')
        try:
            client = docker.from_env()
            # use low-level ping to verify connectivity
            try:
                client.api.ping()
                return True
            except Exception:
                return False
        except Exception as err:
            logging.debug(f'docker.from_env() failed: {err}')
            return False

    return False

def create_base_images():
    """
    Creates the base images for the emulator only for the host CPU arch.
    """
    host_arch = get_host_architecture()
    logging.info(f"Host architecture detected: {host_arch}")
    for arch in ("x86_64", "arm64"):
        if arch == host_arch:
            logging.info(f"Building base image for {arch}")
            if not _is_docker_available():
                raise RuntimeError("Docker daemon not available. Start Docker Desktop / Docker Engine and re-run this script.")
            build_container_image(
                f"fmd-emulator_{arch}",
                f"linux/{arch}",
                f"{EMULATOR_DOCKERFILE_BASE_ABS_PATH}{arch}"
            )


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


def process_images(input_dir, docker_repo_url, repository_username, build_local, skip_push=False):
    start_time = time.time()
    if not check_if_base_images_exists():
        create_base_images()

    emulator_zip_file_list = get_image_file_list_form_disk(input_dir)
    logging.info(f"Processing images: {len(emulator_zip_file_list)}")
    logging.info(f"build_local={build_local} skip_push={skip_push}")

    # Determine worker count (can be changed by setting process_images._workers before calling)
    worker_count = getattr(process_images, '_workers', max(1, multiprocessing.cpu_count()))

    # If pushing remote, prompt for password once here so workers don't prompt
    repository_password = None
    if not build_local:
        repository_password = get_repo_password(repository_username)

    # Prepare result directory
    results_dir = os.path.join(ROOT_PATH, 'results', 'emulator_image_processing')
    os.makedirs(results_dir, exist_ok=True)

    # Prepare tasks for worker processes
    task_list = []
    for p in emulator_zip_file_list:
        task_list.append((p, docker_repo_url, repository_username, repository_password, build_local, results_dir))

    # Cap worker_count to number of images to avoid oversubscription
    worker_count = min(worker_count, max(1, len(task_list)))

    if worker_count == 1:
        aggregate = [process_single_image(t) for t in task_list]
    else:
        logging.info(f"Starting multiprocessing pool with {worker_count} workers")
        with multiprocessing.Pool(processes=worker_count) as pool:
            aggregate = pool.map(process_single_image, task_list)

    # Summarize
    successes = [r for r in aggregate if r.get('success')]
    failures = [r for r in aggregate if not r.get('success')]
    logging.info(f"Finished processing images. Successful: {len(successes)} Failed: {len(failures)}")

    # If we're pushing to a remote registry, perform pushes in parallel from the main process.
    # The `skip_push` flag allows skipping the push step even when images were built from non-local sources.
    if not build_local and not skip_push and successes:
        # Authenticate once before pushing (so workers don't race on docker login)
        if not repository_password:
            raise RuntimeError("Repository password not provided; cannot push")
        logging.info("Authenticating to docker registry before parallel pushes")
        authenticate_docker_registry(docker_repo_url, repository_username, repository_password)

        # Build a list of tags to push
        tags_to_push = [r['tag'] for r in successes]
        logging.info(f"Preparing to push {len(tags_to_push)} images in parallel")

        # Limit push concurrency to avoid saturating network / registry limits
        max_push_workers = min(8, max(1, multiprocessing.cpu_count()))
        with ThreadPoolExecutor(max_workers=max_push_workers) as push_ex:
            push_futures = {push_ex.submit(push_container_image, docker_repo_url, tag): tag for tag in tags_to_push}
            for fut in as_completed(push_futures):
                tag = push_futures[fut]
                try:
                    fut.result()
                    logging.info(f"Pushed image: {tag}")
                except Exception as e:
                    logging.exception(f"Failed to push image {tag}: {e}")

    # Final aggregated summary for process_images
    end_time = time.time()
    elapsed_time_seconds = end_time - start_time
    elapsed_time_minutes = elapsed_time_seconds / 60.0
    summary = {
        'mode': 'local' if build_local else 'remote',
        'elapsed_seconds': elapsed_time_seconds,
        'elapsed_minutes': elapsed_time_minutes,
        'total_images_processed': len(aggregate),
        'successful_images_count': len(successes),
        'failed_images_count': len(failures),
        'successful_tags': [r.get('tag') for r in successes],
        'failed_images': [{'image': r.get('image'), 'error': r.get('error')} for r in failures]
    }

    # Write summary to results directory for easy inspection
    # try:
    #     os.makedirs(os.path.join(ROOT_PATH, 'results'), exist_ok=True)
    #     with open(os.path.join(ROOT_PATH, 'results', 'process_images_summary.json'), 'w', encoding='utf-8') as sf:
    #         json.dump(summary, sf, indent=2)
    # except Exception:
    #     logging.exception('Failed to write process_images_summary.json')

    logging.info('Aggregated summary: %s', summary)
    return summary


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
    parser.add_argument("-w",
                        "--workers",
                        type=int,
                        required=False,
                        default=None,
                        help="Number of parallel workers to build images. Defaults to CPU count.")
    parser.add_argument("--download-workers",
                        type=int,
                        required=False,
                        default=None,
                        help="Number of parallel download worker threads to use (overrides automatic default).")
    parser.add_argument("--aria2-connections",
                        type=int,
                        required=False,
                        default=4,
                        help="Number of connections per aria2c instance when aria2c is used (default: 4).")
    parser.add_argument("--file-list-file",
                        type=str,
                        required=False,
                        help="Path to a file that contains a comma-separated list of filenames to download from the repository."
                             " The file may contain quoted names (CSV style) or multiple rows.")
    parser.add_argument('--skip-push', action='store_true', required=False,
                        help='If set, built images will NOT be pushed to the remote docker registry (useful for testing).')
    parser.add_argument('-b', '--batch-size',
                        type=int,
                        required=False,
                        default=20,
                        help='Maximum number of images to download and build per batch (default: 20).')
    parser.add_argument('--max-active-jobs',
                        type=int,
                        required=False,
                        default=0,
                        help='Maximum number of active (PENDING + PROCESSING) jobs in job_queue before starting the next batch (default: 0).')
    parser.add_argument('--min-free-gb',
                        type=float,
                        required=False,
                        default=50.0,
                        help='Minimum free disk space in GiB required before downloading/building (default: 50.0).')
    parser.add_argument('--keep-zip',
                        action='store_true',
                        required=False,
                        default=False,
                        help='If set, keeps downloaded .zip files in --input-dir instead of deleting them after extraction.')
    return parser.parse_args()


def get_free_space_gb(paths):
    """Returns (min_free_gb, min_path) across all existing paths (or their nearest existing parent)."""
    min_free_gb = None
    min_path = None
    for p in paths:
        if not p:
            continue
        check_path = os.path.abspath(p)
        while check_path and not os.path.exists(check_path):
            parent = os.path.dirname(check_path)
            if parent == check_path:
                break
            check_path = parent
        if os.path.exists(check_path):
            try:
                usage = shutil.disk_usage(check_path)
                free_gb = usage.free / (1024 ** 3)
                if min_free_gb is None or free_gb < min_free_gb:
                    min_free_gb = free_gb
                    min_path = check_path
            except Exception as e:
                logging.debug(f"Could not check disk usage for {check_path}: {e}")
    return (min_free_gb if min_free_gb is not None else float('inf')), (min_path or '.')


def wait_for_disk_space(paths, min_free_gb, poll_interval=15):
    """Blocks until free disk space on all checked paths is at least min_free_gb, or no active jobs remain to free space."""
    if not min_free_gb or min_free_gb <= 0:
        return
    pruned_once = False
    while True:
        free_gb, min_path = get_free_space_gb(paths)
        if free_gb >= min_free_gb:
            return
        active_jobs = job_queue.get_active_job_count()
        if active_jobs == 0:
            if not pruned_once:
                logging.warning(
                    f"Free disk space on {min_path} is {free_gb:.2f} GiB (< {min_free_gb:.2f} GiB) with 0 active jobs. Pruning Docker builder cache..."
                )
                clear_docker_builder()
                pruned_once = True
                free_gb, min_path = get_free_space_gb(paths)
                if free_gb >= min_free_gb:
                    return
            logging.warning(
                f"Free disk space on {min_path} is {free_gb:.2f} GiB (< {min_free_gb:.2f} GiB), but 0 active jobs remain in queue to free space. Proceeding cautiously."
            )
            return
        logging.info(
            f"Disk space low on {min_path} ({free_gb:.2f} GiB free < {min_free_gb:.2f} GiB required). "
            f"Waiting for consumer daemon to finish active jobs ({active_jobs} active)..."
        )
        time.sleep(poll_interval)


def wait_for_batch_capacity(batch_job_ids, max_active_jobs, paths, min_free_gb, poll_interval=10):
    """Waits between batches until active jobs in the current batch drop to <= max_active_jobs and disk space is sufficient."""
    max_active = max(0, int(max_active_jobs or 0))
    while True:
        # Check active jobs for this batch specifically (avoids deadlock if stale rows existed from an older dead run,
        # while still respecting the queue if batch_job_ids is empty).
        if batch_job_ids is not None:
            active_count = job_queue.get_active_job_count(batch_job_ids)
        else:
            active_count = job_queue.get_active_job_count()
        status_counts = job_queue.get_queue_status_counts()
        if active_count <= max_active:
            break
        logging.info(
            f"Waiting for consumer daemon to process current batch: {active_count} batch job(s) still active "
            f"(threshold <= {max_active} | queue totals: {status_counts})..."
        )
        time.sleep(poll_interval)

    logging.info("Batch execution threshold reached. Pruning Docker build cache before next batch...")
    clear_docker_builder()
    wait_for_disk_space(paths, min_free_gb, poll_interval=poll_interval)


def enqueue_single_image(image_name, output_file=None):
    """Immediately pushes a single built image to job_queue and appends it to docker_images.txt."""
    job_queue.init_db()
    job_id = job_queue.push_job(image_name)
    logging.info(f"Pushed {image_name} to the job queue (job_id={job_id}).")
    if output_file:
        try:
            with open(output_file, "a", encoding="utf-8") as f:
                f.write(f"{image_name}\n")
        except Exception as e:
            logging.warning(f"Failed to append {image_name} to {output_file}: {e}")
    return job_id


def enqueue_images(successfully_built_images):
    # Initialize the database (safe to call multiple times)
    job_queue.init_db()
    job_ids = []

    for image in successfully_built_images:
        logging.info(f"Pushing {image} to the job queue.")
        job_ids.append(job_queue.push_job(image))

    logging.info("All built images have been queued for processing.")
    return job_ids


def write_image_names_to_file():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_file = os.path.join(script_dir, "docker_images.txt")

    result = subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}"],
        capture_output=True,
        text=True,
        check=True
    )

    # Filter added: only keep lines that are not empty AND contain the specific substring
    successfully_built_images = [
        line.strip() for line in result.stdout.splitlines()
        if line.strip() and "sdk_phone64_arm64" in line
    ]

    with open(output_file, "w") as f:
        for image in successfully_built_images:
            f.write(f"{image}\n")

    print(f"Captured {len(successfully_built_images)} images.")
    return successfully_built_images


def main():
    args = parse_arguments()

    # If workers provided, set attribute for process_images
    if args.workers:
        process_images._workers = max(1, int(args.workers))

    if not args.create_local:
        clear_environment(args.input_dir)
        # Ensure the input directory exists for downloads (clear_environment may have removed it)
        os.makedirs(args.input_dir, exist_ok=True)
        if not args.repository_url or not args.docker_repo_url or not args.repository_username:
            raise ValueError("Repository URL, Docker repository URL and repository username must be provided.")
        if not args.input_dir:
            raise ValueError("Download destination must be provided.")
        # Read requested file list from a file (CSV-style). The file may contain
        # quoted entries and multiple rows; use the csv module to parse robustly.
        if args.file_list_file:
            try:
                with open(args.file_list_file, 'r', encoding='utf-8') as ff:
                    reader = csv.reader(ff)
                    # flatten rows and strip quotes/whitespace
                    entries = [item.strip() for row in reader for item in row if item is not None]
                    # remove surrounding quotes if present and filter empties
                    file_list = [e.strip().strip('"').strip("'") for e in entries if e.strip()]
            except Exception as e:
                raise RuntimeError(f"Failed to read file list from {args.file_list_file}: {e}")
        else:
            file_list = []
        filtered_image_list = get_filtered_emulator_image_list(args.repository_url, file_list)
        # Download and build images in batches to prevent filling up disk storage.
        download_failed = []
        download_success = []
        aggregate = []
        successful_images = []
        failed_images = []

        # Prepare result directory and repository password before downloads/builds
        results_dir = os.path.join(ROOT_PATH, 'results', 'emulator_image_processing')
        os.makedirs(results_dir, exist_ok=True)

        script_dir = os.path.dirname(os.path.abspath(__file__))
        docker_images_file = os.path.join(script_dir, "docker_images.txt")
        with open(docker_images_file, "w", encoding="utf-8") as f:
            pass

        job_queue.init_db()

        repository_password = None
        if not args.create_local and not args.skip_push:
            repository_password = get_repo_password(args.repository_username)
            logging.info("Authenticating to docker registry before batch processing")
            authenticate_docker_registry(args.docker_repo_url, args.repository_username, repository_password)

        start_time = time.time()

        # Ensure base images exist before starting any builds
        if not check_if_base_images_exists():
            create_base_images()

        if filtered_image_list:
            batch_size = max(1, int(args.batch_size)) if getattr(args, 'batch_size', None) else 20
            min_free_gb = float(getattr(args, 'min_free_gb', 50.0) or 0.0)
            max_active_jobs = max(0, int(getattr(args, 'max_active_jobs', 0) or 0))
            delete_zip = not getattr(args, 'keep_zip', False)
            disk_check_paths = [args.input_dir, ROOT_PATH, "/var/lib/docker"]

            if args.download_workers and int(args.download_workers) > 0:
                download_max_workers = min(batch_size, len(filtered_image_list), int(args.download_workers))
            else:
                download_max_workers = min(batch_size, len(filtered_image_list), max(4, multiprocessing.cpu_count() * 4))

            configured_workers = args.workers if getattr(args, 'workers', None) else multiprocessing.cpu_count()
            build_max_workers = min(batch_size, len(filtered_image_list), max(1, int(configured_workers)))

            total_downloads = len(filtered_image_list)
            batches = [filtered_image_list[i:i + batch_size] for i in range(0, total_downloads, batch_size)]
            total_batches = len(batches)

            logging.info(
                f"Processing {total_downloads} images in {total_batches} batch(es) of up to {batch_size} "
                f"(download_workers={download_max_workers}, build_workers={build_max_workers}, "
                f"min_free_gb={min_free_gb}, max_active_jobs={max_active_jobs}, delete_zip={delete_zip})"
            )

            lock = threading.Lock()
            downloaded_count = 0
            failed_download_count = 0
            build_submitted_count = 0
            builds_completed_count = 0
            failed_build_count = 0

            for batch_idx, batch_assets in enumerate(batches, start=1):
                logging.info(f"=== Starting Batch {batch_idx}/{total_batches} ({len(batch_assets)} images) ===")
                wait_for_disk_space(disk_check_paths, min_free_gb)

                batch_build_futures = []
                batch_job_ids = []

                with ThreadPoolExecutor(max_workers=download_max_workers) as dl_ex, \
                        ThreadPoolExecutor(max_workers=build_max_workers) as build_ex:

                    def _download_asset(asset):
                        try:
                            wait_for_disk_space(disk_check_paths, min_free_gb)
                            dest_file = os.path.join(args.input_dir, asset['path'])
                            os.makedirs(os.path.dirname(dest_file), exist_ok=True)
                            logging.info(f"[Batch {batch_idx}/{total_batches}] Downloading {asset['path']} -> {dest_file}")
                            download_file(asset['downloadUrl'], dest_file, connections=args.aria2_connections)
                            return (True, asset['path'], dest_file, None)
                        except Exception as e:
                            logging.exception(f"Failed to download {asset.get('path')}: {e}")
                            return (False, asset.get('path'), None, str(e))

                    dl_futures = {dl_ex.submit(_download_asset, asset): asset for asset in batch_assets}

                    report_interval = 30.0
                    stop_event = threading.Event()

                    def _reporter():
                        while not stop_event.is_set():
                            stop_event.wait(report_interval)
                            with lock:
                                pending_downloads = sum(1 for f in dl_futures if not f.done())
                                pending_builds = sum(1 for b in batch_build_futures if not b.done())
                                d_done = downloaded_count
                                d_failed = failed_download_count
                                b_sub = build_submitted_count
                                b_done = builds_completed_count
                                b_failed = failed_build_count
                            logging.info(
                                "[Batch %d/%d] Progress summary: downloads completed=%d failed=%d pending=%d | builds submitted=%d completed=%d failed=%d pending=%d",
                                batch_idx, total_batches, d_done, d_failed, pending_downloads, b_sub, b_done, b_failed, pending_builds
                            )

                    reporter_thread = threading.Thread(target=_reporter, name=f'emulator-image-reporter-b{batch_idx}', daemon=True)
                    reporter_thread.start()

                    # As downloads in this batch complete, immediately schedule build tasks
                    for fut in as_completed(dl_futures):
                        ok, name, path, err = fut.result()
                        with lock:
                            if ok:
                                downloaded_count += 1
                            else:
                                failed_download_count += 1
                        if ok:
                            download_success.append((name, path))
                            logging.info(
                                f"[Batch {batch_idx}/{total_batches}] Download complete for {name}; submitting build task "
                                f"({downloaded_count}/{total_downloads} total downloads completed, {failed_download_count} failed)"
                            )
                            task = (path, args.docker_repo_url, args.repository_username, repository_password, args.create_local, results_dir, delete_zip)
                            build_fut = build_ex.submit(process_single_image, task)
                            batch_build_futures.append(build_fut)
                            with lock:
                                build_submitted_count += 1
                            logging.info(
                                f"[Batch {batch_idx}/{total_batches}] Build queue size: "
                                f"{sum(1 for b in batch_build_futures if not b.done())} (submitted {build_submitted_count})"
                            )
                        else:
                            download_failed.append((name, err))

                    logging.info(
                        f"[Batch {batch_idx}/{total_batches}] Batch downloads completed. "
                        f"Waiting for {len(batch_build_futures)} build task(s) in this batch to finish..."
                    )
                    for bf in as_completed(batch_build_futures):
                        try:
                            res = bf.result()
                            aggregate.append(res)
                            with lock:
                                builds_completed_count += 1
                            tag = res.get('tag')
                            logging.info(
                                f"[Batch {batch_idx}/{total_batches}] Build completed ({builds_completed_count}/{build_submitted_count}) for {tag}."
                            )
                            if res.get('success') and tag:
                                successful_images.append(tag)
                                # If remote push is enabled, push before enqueuing so consumer.py does not remove the local image first
                                if not args.create_local and not args.skip_push:
                                    try:
                                        push_container_image(args.docker_repo_url, tag)
                                        logging.info(f"[Batch {batch_idx}/{total_batches}] Pushed image: {tag}")
                                    except Exception as push_err:
                                        logging.exception(f"[Batch {batch_idx}/{total_batches}] Failed to push image {tag}: {push_err}")
                                job_id = enqueue_single_image(tag, output_file=docker_images_file)
                                batch_job_ids.append(job_id)
                            else:
                                with lock:
                                    failed_build_count += 1
                                failed_images.append({'image': res.get('image'), 'error': res.get('error')})
                        except Exception as e:
                            with lock:
                                failed_build_count += 1
                            logging.exception(f"Build task failed: {e}")

                    stop_event.set()
                    try:
                        reporter_thread.join(timeout=5.0)
                    except Exception:
                        pass

                logging.info(
                    f"=== Completed Batch {batch_idx}/{total_batches}: queued {len(batch_job_ids)} image(s) for execution ==="
                )
                if batch_idx < total_batches:
                    wait_for_batch_capacity(
                        batch_job_ids=batch_job_ids,
                        max_active_jobs=max_active_jobs,
                        paths=disk_check_paths,
                        min_free_gb=min_free_gb,
                    )

        logging.info(f"Downloaded {len(download_success)} images, {len(download_failed)} failed downloads")

        if not aggregate and not download_success:
            logging.error("No images were downloaded and built successfully; aborting processing")
            return

        # Write timing/log info for processing duration
        end_time = time.time()
        elapsed_time_seconds = end_time - start_time
        elapsed_time_minutes = elapsed_time_seconds / 60
        with open("results_docker_emulator_image_creation.log", "w") as log_file:
            log_file.write(
                f"Processing images took {elapsed_time_seconds:.2f} seconds ({elapsed_time_minutes:.2f} minutes).\n")

        logging.info(f"Finished processing images. Successful images: {successful_images}. Failed images: {failed_images}.")
    else:
        logging.info("Skipping download of emulator images.")
        summary = process_images(args.input_dir, args.docker_repo_url, args.repository_username, args.create_local, skip_push=args.skip_push)
        # Print final aggregated summary for create_local path
        try:
            logging.info('Final aggregated summary: elapsed=%.2fs (%.2f minutes) | total=%d | success=%d | failed=%d',
                         summary.get('elapsed_seconds', 0.0),
                         summary.get('elapsed_minutes', 0.0),
                         summary.get('total_images_processed', 0),
                         summary.get('successful_images_count', 0),
                         summary.get('failed_images_count', 0))
            if summary.get('failed_images'):
                logging.info('Short failure report: %s', summary.get('failed_images'))
        except Exception:
            logging.exception('Failed to log final aggregated summary for local path')
        return

    # Final aggregated summary for the streaming pipeline
    try:
        aggregate_summary = {
            'mode': 'streaming',
            'elapsed_seconds': elapsed_time_seconds,
            'elapsed_minutes': elapsed_time_minutes,
            'downloads_total': total_downloads if 'total_downloads' in locals() else None,
            'downloads_success': len(download_success) if 'download_success' in locals() else None,
            'downloads_failed': len(download_failed) if 'download_failed' in locals() else None,
            'builds_submitted': build_submitted_count if 'build_submitted_count' in locals() else None,
            'builds_completed': builds_completed_count if 'builds_completed_count' in locals() else None,
            'builds_failed': failed_build_count if 'failed_build_count' in locals() else None,
            'successful_images': successful_images if 'successful_images' in locals() else [],
            'failed_images': failed_images if 'failed_images' in locals() else []
        }
        # write to results
        #try:
        #    os.makedirs(os.path.join(ROOT_PATH, 'results'), exist_ok=True)
        #    with open(os.path.join(ROOT_PATH, 'results', 'aggregate_summary.json'), 'w', encoding='utf-8') as af:
        #        json.dump(aggregate_summary, af, indent=2)
        #except Exception:
        #     logging.exception('Failed to write aggregate_summary.json')

        # Human-readable logging
        logging.info('FINAL AGGREGATED SUMMARY: elapsed=%.2fs (%.2f minutes) | downloads: total=%s success=%s failed=%s | builds: submitted=%s completed=%s failed=%s',
                     aggregate_summary.get('elapsed_seconds'),
                     aggregate_summary.get('elapsed_minutes'),
                     aggregate_summary.get('downloads_total'),
                     aggregate_summary.get('downloads_success'),
                     aggregate_summary.get('downloads_failed'),
                     aggregate_summary.get('builds_submitted'),
                     aggregate_summary.get('builds_completed'),
                     aggregate_summary.get('builds_failed'))

        if aggregate_summary.get('failed_images'):
            logging.info('Short failure report (failed images and errors): %s', aggregate_summary.get('failed_images'))
    except Exception:
        logging.exception('Failed to compose final aggregated summary for streaming pipeline')


if __name__ == "__main__":
    main()