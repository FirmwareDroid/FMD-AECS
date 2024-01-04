import os
from jinja2 import Environment, FileSystemLoader
import argparse

COMPOSE_TEMPLATE_NAME = "docker-compose.yaml"
EMULATOR_TEMPLATE_NAME = "docker_emulator.txt"
OUTPUT_FILENAME = "docker-compose.yaml"
ENVOY_MATCH_TEMPLATE_NAME = "envoy_match.txt"
ENVOY_CLUSTER_TEMPLATE_NAME = "envoy_cluster.txt"
ENVOY_OUTPUT_NAME = "envoy.yaml"


def main():
    """
    Command-line tool to create all necessary file to start the service with variable container sizes for the Android
    emulator.
    """
    parser = argparse.ArgumentParser(
        prog="create_startup_scripts.py",
        description="Creates necessary files to startup the proxy service. "
                    "A new docker-compose YAML file will be written to the current working directory.",
        add_help=True)

    parser.add_argument("-n",
                        "--instance-count",
                        type=int,
                        default=1,
                        help="Number of emulator instances to create.")
    parser.add_argument("-v",
                        "--volume-path",
                        nargs="?",
                        default="/home/suth/aosp_images/12/",
                        help="Path to the AOSP source code on the host machine.")
    parser.add_argument("-g",
                        "--grpc-start-port",
                        type=int,
                        default=8554,
                        help="Starting port for the grpc service.")
    parser.add_argument("-a",
                        "--adb-start-port",
                        type=int,
                        default=5555,
                        help="Starting port for the adb service.")
    args = parser.parse_args()

    template_variables_dict = {"service_name": "android_emulator_",
                               "container_name": "android_emulator_",
                               "host_volume_path": args.volume_path,
                               "grpc_port_host": args.grpc_start_port,
                               "adb_port_host": args.adb_start_port,
                               }
    environment = Environment(loader=FileSystemLoader("templates/"))
    create_docker_template(template_variables_dict, args.instance_count, environment)
    create_envoy_template(template_variables_dict, args.instance_count, environment)


def create_docker_template(template_variables_dict, instance_count, environment):
    """
    Creates a docker compose file with a container for every Android emulator.
    Args:
        template_variables_dict: dict - variables to configure the containers
        instance_count: int - number of containers to insert.
        environment: jinja2 template engine
    """
    try:
        template_path = os.path.join("./", COMPOSE_TEMPLATE_NAME)
        os.remove(template_path)
    except Exception:
        pass

    emulator_template_content_list = []
    for x in range(0, instance_count):
        template = environment.get_template(EMULATOR_TEMPLATE_NAME)

        service_name = template_variables_dict["service_name"] + str(x)
        container_name = template_variables_dict["container_name"] + str(x)
        host_volume_path = template_variables_dict["host_volume_path"] + str(x)
        grpc_port_host = template_variables_dict["grpc_port_host"] + x
        adb_port_host = template_variables_dict["adb_port_host"] + x
        content = template.render(
            service_name=service_name,
            container_name=container_name,
            host_volume_path=host_volume_path,
            grpc_port_host=grpc_port_host,
            adb_port_host=adb_port_host
        )
        emulator_template_content_list.append(content)

    template = environment.get_template(COMPOSE_TEMPLATE_NAME)
    content = template.render(
        emulator_content_list=emulator_template_content_list,
    )

    with open(OUTPUT_FILENAME, mode="w", encoding="utf-8") as message:
        message.write(content)
        print(f"... wrote {OUTPUT_FILENAME}")


def create_envoy_template(template_variables_dict, instance_count, environment):
    """
    Creates an envoy.yaml configuration path with routes and clusters for every emulator
    Args:
        template_variables_dict: dict - variables to insert into the template.
        instance_count: int - number of emulators
        environment: jinja2 template engine

    """
    output_path = os.path.join("./env/envoy/", ENVOY_OUTPUT_NAME)
    try:
        os.remove(output_path)
    except Exception:
        pass

    envoy_match_list = []
    cluster_config_list = []
    for x in range(0, instance_count):
        grpc_port_host = template_variables_dict["grpc_port_host"] + x

        envoy_match_template = environment.get_template(ENVOY_MATCH_TEMPLATE_NAME)
        content = envoy_match_template.render(
            emulator_id=x,
        )
        envoy_match_list.append(content)
        envoy_cluster_template = environment.get_template(ENVOY_CLUSTER_TEMPLATE_NAME)
        content = envoy_cluster_template.render(
            emulator_id=x,
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
