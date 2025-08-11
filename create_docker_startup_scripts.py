import logging
import sys
import os
import argparse
from jinja2 import Environment, FileSystemLoader

logging.basicConfig(stream=sys.stdout, level=logging.INFO)

COMPOSE_TEMPLATE_NAME = "docker-compose.yaml"
EMULATOR_TEMPLATE_NAME = "docker_emulator.txt"
OUTPUT_FILENAME = "docker-compose.yaml"
ENVOY_MATCH_TEMPLATE_NAME = "envoy_match.txt"
ENVOY_CLUSTER_TEMPLATE_NAME = "envoy_cluster.txt"
ENVOY_OUTPUT_NAME = "envoy.yaml"
PLATFORM_X86_64 = "linux/amd64"
PLATFORM_ARM64 = "linux/arm64"


def main():
    """
    Command-line tool to create all necessary files to start the service with variable container sizes for the Android
    emulator.
    """
    parser = argparse.ArgumentParser(
        prog="create_startup_scripts.py",
        description="Creates necessary files to startup the envoy proxy service. "
                    "A new docker-compose YAML file will be written to the current working directory. Examples:"
                    "\n"
                    "python create_docker_startup_scripts.py.py -c linux/arm64"
                    "python create_docker_startup_scripts.py.py -g 8554 -a 5555 -s 2222 -c linux/arm64",
        add_help=True)
    parser.add_argument("-g",
                        "--grpc-start-port",
                        type=int,
                        default=8554,
                        help="Starting port for the grpc service. Default is 8554.")
    parser.add_argument("-a",
                        "--adb-start-port",
                        type=int,
                        default=5555,
                        help="Starting port for the adb service. Default is 5555.")
    parser.add_argument("-s",
                        "--ssh-start-port",
                        type=int,
                        default=2222,
                        help="Starting port for the ssh service. Default is 2222.")
    parser.add_argument("-c",
                        "--cpu-arch",
                        type=str,
                        default=PLATFORM_ARM64,
                        choices=[PLATFORM_X86_64, PLATFORM_ARM64],
                        help="Defines the CPU architecture for docker. Default is linux/amd64.")
    parser.add_argument("-d",
                        "--debug",
                        action="store_true",
                        default=False,
                        help="Defines the debug mode for docker.")
    args = parser.parse_args()

    if args.cpu_arch not in [PLATFORM_X86_64, PLATFORM_ARM64]:
        logging.info(f"CPU architecture is set to {args.cpu_arch}.")
        raise ValueError("CPU architecture must be either linux/amd64 or linux/arm64.")
    logging.info("Creating docker-compose.yaml file for CPU architecture: %s", args.cpu_arch)

    if args.debug:
        envoy_port_mapping = ["- \"4443:443\""]
    else:
        envoy_port_mapping = ["- \"443:443\""]

    template_variables_dict = {"service_name": "android_emulator_",
                               "container_name": "android_emulator_",
                               "grpc_port_host": args.grpc_start_port,
                               "adb_port_host": args.adb_start_port,
                               "platform": args.cpu_arch,
                               "debug": args.debug,
                               "ssh_port_host": args.ssh_start_port,
                               "envoy_port_mapping": envoy_port_mapping
                               }
    environment = Environment(loader=FileSystemLoader("templates/"))
    docker_image_name_list = get_docker_images_names()
    create_docker_compose_file(template_variables_dict, environment, docker_image_name_list)
    create_envoy_template(template_variables_dict, environment, docker_image_name_list)


def get_docker_images_names():
    image_name_list = []
    if not os.path.exists("./docker_images.txt"):
        raise FileNotFoundError("docker_images.txt not found.")
    with open("./docker_images.txt", mode="r", encoding="utf-8") as file:
        for line in file:
            image_name_list.append(line.strip())
    if len(image_name_list) == 0:
        raise ValueError("No docker images found in docker_images.txt.")
    return image_name_list


def create_docker_compose_file(template_variables_dict, environment, docker_image_name_list):
    """
    Creates a docker compose file with a container for every Android emulator.

    """
    try:
        template_path = os.path.join("./", COMPOSE_TEMPLATE_NAME)
        os.remove(template_path)
    except Exception:
        pass

    emulator_template_content_list = []
    x = 0
    for image_name in docker_image_name_list:
        template = environment.get_template(EMULATOR_TEMPLATE_NAME)
        service_name = template_variables_dict["service_name"] + str(x)
        container_name = template_variables_dict["container_name"] + str(x)
        grpc_port_host = template_variables_dict["grpc_port_host"] + x
        adb_port_host = template_variables_dict["adb_port_host"] + x
        ssh_port_host = template_variables_dict["ssh_port_host"] + x
        platform = template_variables_dict["platform"]
        optional_settings = ["devices: [/dev/kvm]"]
        if template_variables_dict["debug"]:
            optional_settings.append("command: sleep infinity")
        content = template.render(
            service_name=service_name,
            container_name=container_name,
            image_name=image_name,
            grpc_port_host=grpc_port_host,
            adb_port_host=adb_port_host,
            ssh_port_host=ssh_port_host,
            platform=platform,
            optional_settings=optional_settings,
        )
        emulator_template_content_list.append(content)
        x += 1

    template = environment.get_template(COMPOSE_TEMPLATE_NAME)
    content = template.render(
        emulator_content_list=emulator_template_content_list,
        platform=template_variables_dict["platform"],
        envoy_port_mapping=template_variables_dict["envoy_port_mapping"],
    )

    with open(OUTPUT_FILENAME, mode="w", encoding="utf-8") as message:
        message.write(content)
        print(f"... wrote {OUTPUT_FILENAME}")


def create_envoy_template(template_variables_dict, environment, docker_image_name_list):
    """
    Creates an envoy.yaml configuration path with routes and clusters for every emulator


    """
    output_path = os.path.join("./env/envoy/", ENVOY_OUTPUT_NAME)
    try:
        os.remove(output_path)
    except Exception:
        pass

    envoy_match_list = []
    cluster_config_list = []
    for emulator_id in range(0, len(docker_image_name_list)):
        grpc_port_host = template_variables_dict["grpc_port_host"]

        envoy_match_template = environment.get_template(ENVOY_MATCH_TEMPLATE_NAME)
        content = envoy_match_template.render(
            emulator_id=emulator_id,
        )
        envoy_match_list.append(content)
        envoy_cluster_template = environment.get_template(ENVOY_CLUSTER_TEMPLATE_NAME)
        content = envoy_cluster_template.render(
            emulator_id=emulator_id,
            grpc_port_host=grpc_port_host
        )
        cluster_config_list.append(content)

    template = environment.get_template(ENVOY_OUTPUT_NAME)
    content = template.render(
        envoy_match_list=envoy_match_list,
        cluster_config_list=cluster_config_list,
    )
    with open(output_path, mode="w", encoding="utf-8") as message:
        message.write(content)
        print(f"... wrote {ENVOY_OUTPUT_NAME}")


if __name__ == '__main__':
    main()
