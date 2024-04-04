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
    logging.debug(f"Building docker image for firmware: {tag}, arch: {docker_build_arch}, target: {target_build_arch}")
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
        logging.debug(f"Docker build logs will be written to: {log_path}")
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