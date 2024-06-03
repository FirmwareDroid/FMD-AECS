import json
import logging
import os
import re
import requests
from werkzeug.utils import secure_filename
from string import Template
from tqdm import tqdm
from config import FMD_AUTH_QUERY_TEMPLATE, VERIFY_SSL, FMD_CSRF_URL_TEMPLATE, FMD_AECS_FIRMWARE_QUERY_TEMPLATE, \
    FMD_FIRMWARE_BUILD_FILES_DOWNLOAD_TEMPLATE, FMD_GRAPHQL_URL_TEMPLATE, NEXUS_SERVICE_ENDPOINT


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


def get_firmware_ids(graphql_url, cookies, arch=None):
    """
    Fetches a list of firmware ids to process from the fmd service.

    :param graphql_url: str - fmd api url for graphql.
    :param cookies: str - cookies jar for requests.
    :param arch: str - cpu architecture of the firmware.

    :returns: list(str) - list of firmware ids

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
        aecs_job_list = resp_dict["data"]["aecs_job_list"]
        object_id_list = []
        for aecs_job in aecs_job_list:
            if arch and aecs_job["arch"] != arch:
                continue
            else:
                object_id_list.append(aecs_job["firmware_id_list"])
        if not object_id_list:
            raise RuntimeError("Could not fetch firmware ids.")
    return object_id_list


def download_firmware_build_files(fmd_url, firmware_id, cookies, aosp_packages_abs_path, max_attempts=10):
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
    attempt = 0
    is_successful = False
    while attempt < max_attempts and not is_successful:
        try:
            logging.info(f"Attempt {attempt} to download build file from {download_url}...")
            if output_file_path and os.path.exists(output_file_path):
                current_size = os.path.getsize(output_file_path)
                headers["Range"] = f"bytes={current_size}-"
            response = requests.post(download_url,
                                     data=request_body,
                                     headers=headers,
                                     stream=True,
                                     verify=VERIFY_SSL,
                                     cookies=cookies)
            response.raise_for_status()
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
            is_successful = True
        except Exception as err:
            logging.error(f"Attempt {attempt} failed: {err}")
            if attempt == max_attempts:
                raise RuntimeError(f"Failed to download firmware build files after {max_attempts} attempts.")
        attempt += 1

    if not response or response.status_code not in (200, 206):
        raise RuntimeError(f"Could not download firmware build files. Status code: {response.status_code}")

    logging.info(f"Downloaded firmware build files to {output_file_path}")
    return output_file_path


def get_graphql_url(fmd_url):
    temp_obj = Template(FMD_GRAPHQL_URL_TEMPLATE)
    graphql_url = temp_obj.substitute(url=fmd_url)
    return graphql_url


def upload_image_as_raw(repo_url, username, password, file_path, filename):
    """
    Uploads an image as raw to the given repository.

    :param repo_url: str - URL of the repository.
    :param username: str - Username to authenticate to the repository.
    :param password: str - Password to authenticate to the repository.
    :param file_path: str - Path to the image file.
    :param filename: str - Name of the file to upload.

    :return: bool - True if the upload was successful, False otherwise.

    """
    is_successful = False
    if not repo_url.endswith('/'):
        repo_url = f'{repo_url}/'

    url = f'{repo_url}{filename}'
    logging.info(f'Uploading image {file_path} as raw to {url}')
    with open(file_path, 'rb') as f:
        response = requests.put(url, auth=(username, password), data=f, verify=VERIFY_SSL)

    if response.status_code == 200 or response.status_code == 201:
        logging.info('File uploaded successfully')
        is_successful = True
    else:
        logging.error(f'Failed to upload file: {response.text}')
    return is_successful


def download_file(url, destination):
    """
    Downloads a file from the given URL and saves it to the specified destination.

    :param url: str - URL of the file to download.
    :param destination: str - Path where the downloaded file should be saved.
    """
    response = requests.get(url, stream=True, verify=VERIFY_SSL)

    if response.status_code == 200:
        file_size = int(response.headers.get('Content-Length', 0))
        progress = tqdm(response.iter_content(1024), f'Downloading {url}',
                        total=file_size, unit='B', unit_scale=True, unit_divisor=1024)
        with open(destination, 'wb') as file:
            for data in progress.iterable:
                file.write(data)
                progress.update(len(data))
    else:
        raise RuntimeError(f"Failed to download file. Status code: {response.status_code}")


def fetch_emulator_image_list(repository_url):
    """
    Get all available emulator images from the repository.

    Returns: str - json response of the available emulator images.
    """
    if not repository_url.endswith('/'):
        repository_url = f'{repository_url}/'
    url = f"{repository_url}{NEXUS_SERVICE_ENDPOINT}"
    logging.info(f"Fetching emulator images from {url}")
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
    }
    data = ("{\"action\":\"coreui_Browse\",\"method\":\"read\",\"data\":"
            "[{\"repositoryName\":\"emulator-images\",\"node\":\"/\"}],\"type\":\"rpc\",\"tid\":5}")
    response = requests.post(url, headers=headers, data=data, verify=VERIFY_SSL)
    if response.status_code != 200:
        raise RuntimeError(f"Could not fetch emulator images. Status code: {response.status_code}")
    return response.json()
