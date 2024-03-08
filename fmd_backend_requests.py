import json
import logging
import os
import re
import requests
from werkzeug.utils import secure_filename
from string import Template
from tqdm import tqdm
from config import FMD_AUTH_QUERY_TEMPLATE, VERIFY_SSL, FMD_CSRF_URL_TEMPLATE, FMD_AECS_FIRMWARE_QUERY_TEMPLATE, \
    FMD_APP_ID_QUERY_TEMPLATE, FMD_FIRMWARE_BUILD_FILES_DOWNLOAD_TEMPLATE, FMD_GRAPHQL_URL_TEMPLATE


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
        try:
            resp_dict = response.json()
            jwt_token = resp_dict["data"]["tokenAuth"]["token"]
            if not jwt_token:
                raise RuntimeError("Could not authenticate.")
        except KeyError:
            raise RuntimeError("Could not authenticate. Maybe wrong username or password?")
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
        resp_dict = response.json()
        object_id_list = resp_dict["data"]["aecs_firmware_id_list"]
        if not object_id_list:
            raise RuntimeError("Could not fetch firmware ids.")
    return object_id_list


def get_android_app_ids(graphql_url, firmware_id, cookies):
    """
    Fetches a list of Android app ids from the fmd service.
    Args:
        graphql_url: str - URL to the fmd service api
        firmware_id: str - id of the firmware to fetch the Android apps from.
        cookies: str - cookie-jar for requests.

    Returns: list(str) - list of object id for AndroidApp documents.

    """
    temp_obj = Template(FMD_APP_ID_QUERY_TEMPLATE)
    params = temp_obj.substitute(firmware_id=firmware_id)
    params = json.loads(params)
    headers = {"X-CSRFToken": cookies["csrftoken"], "Referer": graphql_url}
    with requests.post(graphql_url,
                       headers=headers,
                       params=params,
                       cookies=cookies,
                       verify=VERIFY_SSL) as response:
        resp_dict = response.json()
        object_id_list = resp_dict["data"]["android_app_id_list_by_firmware"]
        if not object_id_list:
            raise RuntimeError("Could not fetch Android app ids.")
    return object_id_list


def download_firmware_build_files(fmd_url, android_app_id_list, cookies, aosp_packages_abs_path):
    """
    Downloads the build files for the given Android app (object id) and shows a progress bar of the download.

    :param fmd_url: str - base url of the FirmwareDroid backend.
    :param android_app_id_list: list(str) - document reference to the Android app to download.
    :param cookies: str - cookie jar for http requests.
    :param aosp_packages_abs_path: str - folder of the aosp app packages.

    :return: str - path to the downloaded file.

    """
    temp_obj = Template(FMD_FIRMWARE_BUILD_FILES_DOWNLOAD_TEMPLATE)
    download_url = temp_obj.substitute(url=fmd_url)
    headers = {"X-CSRFToken": cookies["csrftoken"],
               "Referer": fmd_url,
               "Content-Type": "application/json"}
    android_app_dict = {"object_id_list": android_app_id_list}
    android_app_dict = json.dumps(android_app_dict)
    logging.info(f"Downloading firmware build files from {download_url}...this may take a while.")
    response = requests.post(download_url,
                             data=android_app_dict,
                             headers=headers,
                             stream=True,
                             verify=VERIFY_SSL,
                             cookies=cookies)
    if response.status_code != 200:
        raise RuntimeError(f"Could not download firmware build files. Status code: {response.status_code}")
    total_size_in_bytes = int(response.headers.get('Content-Length', 0))
    progress_bar = tqdm(total=total_size_in_bytes, unit='iB', unit_scale=True)
    content_disposition_header = response.headers['Content-Disposition']
    filename_unsafe = re.findall("filename=(.+)", content_disposition_header)[0]
    filename = secure_filename(filename_unsafe)

    output_file_path = os.path.join(aosp_packages_abs_path, filename)
    with open(output_file_path, mode="wb") as file:
        for chunk in response.iter_content(chunk_size=10 * 1024):
            progress_bar.update(len(chunk))
            file.write(chunk)
    progress_bar.close()
    if total_size_in_bytes != 0 and progress_bar.n != total_size_in_bytes:
        print("ERROR, something went wrong downloading the firmware build files")
        print("Continue with remaining firmware.")
    return output_file_path


def get_graphql_url(fmd_url):
    temp_obj = Template(FMD_GRAPHQL_URL_TEMPLATE)
    graphql_url = temp_obj.substitute(url=fmd_url)
    return graphql_url
