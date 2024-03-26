import json
import logging
import os
import re
import requests
from werkzeug.utils import secure_filename
from string import Template
from tqdm import tqdm
from config import FMD_AUTH_QUERY_TEMPLATE, VERIFY_SSL, FMD_CSRF_URL_TEMPLATE, FMD_AECS_FIRMWARE_QUERY_TEMPLATE, \
    FMD_FIRMWARE_BUILD_FILES_DOWNLOAD_TEMPLATE, FMD_GRAPHQL_URL_TEMPLATE


def authenticate_fmd(graphql_url, username, password, csrf_cookie):
    """
    Authenticates to the fmd-service to get a jwt-token.
    Args:
        csrf_cookie: cookie-jar object including the csrf token cookie-
        graphql_url: str - URL to the fmd graphql api.
        username: str - name to use for the authentication
        password: str - password to use to authenticate.

    Returns: str - jwt authentication cookie.
    """
    temp_obj = Template(FMD_AUTH_QUERY_TEMPLATE)
    params = temp_obj.substitute(username=username, password=password)
    params = json.loads(params)
    headers = {"X-CSRFToken": csrf_cookie["csrftoken"], "Referer": graphql_url}
    with requests.post(graphql_url,
                       cookies=csrf_cookie,
                       stream=True,
                       headers=headers,
                       verify=VERIFY_SSL,
                       params=params) as response:
        if response.status_code != 200:
            raise RuntimeError(f"Could not authenticate. Status code: {response.status_code}")
        else:
            resp_dict = response.json()
            jwt_token = resp_dict["data"]["tokenAuth"]["token"]
            if not jwt_token:
                raise RuntimeError("Could not authenticate.")
            auth_cookie = response.cookies
    return auth_cookie


def get_csrf_token(url):
    """
    Fetches a csrf token to make further requests.

    Args:
        url: str - URL to the fmd main service.

    Returns: str - cookie including the csrf token.

    """
    temp_obj = Template(FMD_CSRF_URL_TEMPLATE)
    fetch_url = temp_obj.substitute(url=url)
    with requests.get(fetch_url, verify=VERIFY_SSL) as response:
        if response.status_code != 200:
            raise RuntimeError(f"Could not fetch CSRF-Token. Status code: {response.status_code}")
        resp_dict = response.json()
        csrf_token = resp_dict["csrfToken"]
        if not csrf_token:
            raise RuntimeError("Could not fetch CSRF-Token.")
    return response.cookies


def get_firmware_ids(graphql_url, cookies):
    """
    Fetches a list of firmware ids to process from the fmd service.

    Args:
        graphql_url: str - fmd api url for graphql.
        cookies: str - cookies jar for requests.

    Returns: list(str) - list of firmware ids

    """
    headers = {"X-CSRFToken": cookies["csrftoken"], "Referer": graphql_url}
    params = json.loads(FMD_AECS_FIRMWARE_QUERY_TEMPLATE)
    with requests.post(graphql_url,
                       cookies=cookies,
                       params=params,
                       headers=headers,
                       verify=VERIFY_SSL) as response:
        if response.status_code != 200:
            raise RuntimeError(f"Could not fetch firmware ids. Status code: {response.status_code};"
                               f"response: {response.text}")
        resp_dict = response.json()
        object_id_list = resp_dict["data"]["aecs_firmware_id_list"]
        if not object_id_list:
            raise RuntimeError("Could not fetch firmware ids.")
    return object_id_list


def download_firmware_build_files(fmd_url, firmware_id, cookies, aosp_packages_abs_path, max_attempts=3):
    """
    Downloads the build files for the given Android app (object id) and shows a progress bar of the download.

    :param fmd_url: str - base url of the FirmwareDroid backend.
    :param firmware_id: str - id of the firmware to fetch the Android apps from.
    :param cookies: str - cookie jar for http requests.
    :param aosp_packages_abs_path: str - folder of the aosp app packages.
    :param max_attempts: int - maximum number of download attempts.

    :return: str - path to the downloaded file.

    """
    temp_obj = Template(FMD_FIRMWARE_BUILD_FILES_DOWNLOAD_TEMPLATE)
    download_url = temp_obj.substitute(url=fmd_url)
    headers = {"X-CSRFToken": cookies["csrftoken"],
               "Referer": fmd_url,
               "Content-Type": "application/json"}
    request_body = {"object_id_list": [firmware_id]}
    request_body = json.dumps(request_body)

    content_disposition_header = None
    output_file_path = None
    response = None
    total_size_in_bytes = 0
    for attempt in range(max_attempts):
        try:
            logging.info(f"Attempt {attempt+1} to download build file from {download_url}...")
            if output_file_path and os.path.exists(output_file_path):
                # If the file already exists, get the size and set the Range header
                current_size = os.path.getsize(output_file_path)
                headers["Range"] = f"bytes={current_size}-"
            response = requests.post(download_url,
                                     data=request_body,
                                     headers=headers,
                                     stream=True,
                                     verify=VERIFY_SSL,
                                     cookies=cookies)
            response.raise_for_status()  # Raises a HTTPError if the status is 4xx, 5xx
            if not content_disposition_header:
                content_disposition_header = response.headers['Content-Disposition']
                filename_unsafe = re.findall("filename=(.+)", content_disposition_header)[0]
                filename = secure_filename(filename_unsafe)
                output_file_path = os.path.join(aosp_packages_abs_path, filename)
            total_size_in_bytes = int(response.headers.get('Content-Length', 0))
            progress_bar = tqdm(total=total_size_in_bytes, unit='iB', unit_scale=True)
            logging.info(f"Downloading firmware build files to {output_file_path}...")
            with open(output_file_path, mode="ab") as file:
                for chunk in response.iter_content(chunk_size=10 * 1024):
                    progress_bar.update(len(chunk))
                    file.write(chunk)
            progress_bar.close()
            break  # If the download was successful, exit the loop
        except Exception as err:
            logging.error(f"Attempt {attempt+1} failed: {err}")
            if attempt + 1 == max_attempts:
                raise RuntimeError(f"Failed to download firmware build files after {max_attempts} attempts.")
    if not response or response.status_code not in (200, 206):
        raise RuntimeError(f"Could not download firmware build files. Status code: {response.status_code}")

    logging.info(f"Got firmware build files from {download_url}.")
    if total_size_in_bytes != 0 and os.path.getsize(output_file_path) != total_size_in_bytes:
        print("ERROR, something went wrong downloading the firmware build files")
        print("Continue with remaining firmware.")
    return output_file_path


def get_graphql_url(fmd_url):
    temp_obj = Template(FMD_GRAPHQL_URL_TEMPLATE)
    graphql_url = temp_obj.substitute(url=fmd_url)
    return graphql_url


def upload_image_as_raw(repo_url, firmware_id, docker_repo_username, docker_repo_password, arch, file_path):
    """
    Uploads an image as raw to the given repository.

    :param repo_url: str - URL of the repository.
    :param firmware_id: str - ID of the firmware.
    :param docker_repo_username: str - Username to authenticate to the repository.
    :param docker_repo_password: str - Password to authenticate to the repository.
    :param arch: str - CPU Architecture of the image.
    :param file_path: str - Path to the image file.

    :return: bool - True if the upload was successful, False otherwise.
    """
    is_successful = False
    url = f'{repo_url}/{firmware_id}_{arch}.zip'
    logging.info(f'Uploading image {file_path} as raw to {url}')
    with open(file_path, 'rb') as f:
        response = requests.post(url, auth=(docker_repo_username, docker_repo_password), files={'file': f})

    if response.status_code == 200:
        logging.info('File uploaded successfully')
        is_successful = True
    else:
        logging.error('Failed to upload file')
    return is_successful
