import os
import argparse
from jinja2 import Environment, FileSystemLoader
from getpass import getpass
from fmd_backend_requests import get_csrf_token, authenticate_fmd, get_firmware_ids, get_graphql_url

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
    parser.add_argument("-u", "--fmd-username",
                        type=str,
                        default=None,
                        required=True,
                        help="Username for the authentication to the fmd service.")
    parser.add_argument("-f", "--fmd-url",
                        type=str,
                        default=None,
                        required=True,
                        help="HTTP/HTTPS url to the FMD instance to grab the packages."
                             "Example: https://firmwaredroid.cloudlab.zhaw.ch")
    parser.add_argument("-r", "--docker-repo-url",
                        type=str,
                        default=None,
                        help="Specifies the url to a docker registry, "
                             "where the emulator images will be pushed to. "
                             "Example: 127.0.0.1:8082/repository/docker-test/")
    args = parser.parse_args()

    template_variables_dict = {"service_name": "android_emulator_",
                               "container_name": "android_emulator_",
                               "grpc_port_host": args.grpc_start_port,
                               "adb_port_host": args.adb_start_port,
                               }
    fmd_password = getpass()
    docker_repo_url = args.docker_repo_url.replace("http://", "").replace("https://", "")
    if not docker_repo_url.endswith("/"):
        raise ValueError("--docker-repo-url must end with a slash (/). Example: 127.0.0.1:8082/repository/docker-test/")
    environment = Environment(loader=FileSystemLoader("templates/"))
    firmware_id_list = request_firmware_ids(args.fmd_url, args.fmd_username, fmd_password)
    create_docker_template(template_variables_dict, firmware_id_list, environment, docker_repo_url)
    create_envoy_template(template_variables_dict, firmware_id_list, environment)


def request_firmware_ids(fmd_url, fmd_username, fmd_password):
    """
    Fetches the list of firmware ids from the aecs-job.
    Args:
        fmd_url: str - URL to the fmd backend service.
        fmd_username: str - username to authenticate to the fmd service.
        fmd_password: str - password to authenticate to the fmd service.

    Returns: list(str) - list of firmware ids.

    """
    csrf_cookie = get_csrf_token(fmd_url)
    graphql_url = get_graphql_url(fmd_url)
    cookies = authenticate_fmd(graphql_url, fmd_username, fmd_password, csrf_cookie)
    firmware_id_list = get_firmware_ids(graphql_url, cookies)
    return firmware_id_list


def create_docker_template(template_variables_dict, firmware_id_list, environment, docker_repo_url):
    """
    Creates a docker compose file with a container for every Android emulator.
    Args:
        template_variables_dict: dict - variables to configure the containers'
        environment: jinja2 template engine
    """
    try:
        template_path = os.path.join("./", COMPOSE_TEMPLATE_NAME)
        os.remove(template_path)
    except Exception:
        pass

    emulator_template_content_list = []
    x = 0
    for firmware_id in firmware_id_list:
        template = environment.get_template(EMULATOR_TEMPLATE_NAME)
        image_name = docker_repo_url + firmware_id
        service_name = template_variables_dict["service_name"] + str(x)
        container_name = template_variables_dict["container_name"] + str(x)
        grpc_port_host = template_variables_dict["grpc_port_host"] + x
        adb_port_host = template_variables_dict["adb_port_host"] + x
        content = template.render(
            service_name=service_name,
            container_name=container_name,
            image_name=image_name,
            grpc_port_host=grpc_port_host,
            adb_port_host=adb_port_host
        )
        emulator_template_content_list.append(content)
        x += 1

    template = environment.get_template(COMPOSE_TEMPLATE_NAME)
    content = template.render(
        emulator_content_list=emulator_template_content_list,
    )

    with open(OUTPUT_FILENAME, mode="w", encoding="utf-8") as message:
        message.write(content)
        print(f"... wrote {OUTPUT_FILENAME}")


def create_envoy_template(template_variables_dict, firmware_id_list, environment):
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
    for x in range(0, len(firmware_id_list)):
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
