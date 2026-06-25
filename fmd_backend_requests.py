import base64
import json
import logging
import os
import re
import time
import shutil
import subprocess

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from werkzeug.utils import secure_filename
from string import Template
from tqdm import tqdm
from urllib.parse import urlparse
from config import FMD_AUTH_QUERY_TEMPLATE, VERIFY_SSL, FMD_CSRF_URL_TEMPLATE, FMD_AECS_FIRMWARE_QUERY_TEMPLATE, \
    FMD_FIRMWARE_BUILD_FILES_DOWNLOAD_TEMPLATE, FMD_GRAPHQL_URL_TEMPLATE, NEXUS_SERVICE_ENDPOINT, \
    FMD_APP_MANIFEST_QUERY_TEMPLATE


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
            logging.error(f"Could not authenticate. Status code: {response.status_code}; response: {response.text}")
            raise RuntimeError(f"Could not authenticate. Status code: {response.status_code}")
        else:
            #resp_dict = response.json()
            #jwt_token = resp_dict["data"]["tokenAuth"]["token"]
            #if not jwt_token:
            #    raise RuntimeError("Could not authenticate.")
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


def get_firmware_ids(graphql_url, cookies, arch=None, pk_filter=None):
    """
    Fetches a list of firmware ids to process from the fmd service.

    :param graphql_url: str - fmd api url for graphql.
    :param cookies: str - cookies jar for requests.
    :param arch: str - cpu architecture of the firmware.
    :param pk_filter: str - id of the aecs job to process.

    :returns: list(str) - list of firmware ids

    """
    logging.info("Fetching aecs jobs...")
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
        logging.info(f"Found {len(aecs_job_list)} aecs jobs.")
        object_id_list = []
        for aecs_job in aecs_job_list:
            logging.debug(f"Processing aecs job with pk: {aecs_job['pk']} and arch: {aecs_job['arch']}")
            if (pk_filter and aecs_job["pk"] != pk_filter) or (arch and aecs_job["arch"] != arch):
                logging.debug(f"Skipping aecs job with pk: {aecs_job['pk']} and arch: {aecs_job['arch']} "
                             f"with pk_filter: {pk_filter}")
                continue
            else:
                for firmware_data in aecs_job["firmwareIdList"]['edges']:
                    id_value = firmware_data['node']['id']
                    base64_id = id_value
                    decoded_bytes = base64.b64decode(base64_id)
                    decoded_string = decoded_bytes.decode('utf-8')
                    object_id_list.append(decoded_string.split(":")[1])
        if not object_id_list:
            logging.error("No firmware ids found to process.")
            exit(0)
    logging.info(f"Found ids to process: {object_id_list}")
    return object_id_list


def download_firmware_build_files(fmd_url, firmware_id, cookies, aosp_packages_abs_path,
                                  auth_username=None, auth_password=None, max_attempts=10):
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
    #logging.info(f"Downloading {download_url} with cookies: {cookies.get_dict()}")
    headers = {"X-CSRFToken": cookies["csrftoken"],
               "Referer": fmd_url,
               "Content-Type": "application/json"}
    if cookies and "jwt-session" in cookies.keys():
        headers["Authorization"] = f"Bearer {cookies['jwt-session']}"

    request_body = {"object_id_list": [firmware_id]}
    request_body = json.dumps(request_body)

    content_disposition_header = None
    output_file_path = None
    response = None
    total_size_in_bytes = 0
    attempt = 0
    is_successful = False
    timeout = 30
    while attempt < max_attempts and not is_successful:
        try:
            logging.info(f"Attempt {attempt} to download build file from {download_url}...")
            if output_file_path and os.path.exists(output_file_path):
                current_size = os.path.getsize(output_file_path)
                headers["Range"] = f"bytes={current_size}-"
            # Use an explicit (connect, read) timeout tuple. ReadTimeouts were
            # observed previously — increase the read timeout to allow slower
            # transfers to proceed without aborting prematurely.
            response = requests.post(download_url,
                                     data=request_body,
                                     headers=headers,
                                     stream=True,
                                     verify=VERIFY_SSL,
                                     cookies=cookies,
                                     timeout=(10, 600))
            # If we get a 403 Forbidden, try to re-authenticate (best-effort) and retry.
            if response is not None and response.status_code == 403:
                logging.warning("Received 403 Forbidden while downloading build files. Attempting re-authentication...")
                # Only attempt re-authentication if credentials were provided
                if auth_username and auth_password:
                    try:
                        # Fetch fresh CSRF token and authenticate to update cookies
                        new_csrf = get_csrf_token(fmd_url)
                        graphql_url = get_graphql_url(fmd_url)
                        new_auth_cookies = authenticate_fmd(graphql_url, auth_username, auth_password, new_csrf)
                        # Update cookies and headers for subsequent attempts. Try to update the passed-in
                        # cookie jar in-place so callers see the refreshed cookies as well.
                        try:
                            # If cookies is a cookiejar-like object, update it in-place
                            cookies.clear()
                            for k, v in new_auth_cookies.items():
                                cookies.set(k, v)
                        except Exception:
                            # Fallback: replace local reference
                            cookies = new_auth_cookies
                        headers["X-CSRFToken"] = (cookies.get("csrftoken") if hasattr(cookies, 'get') else headers.get("X-CSRFToken"))
                        if cookies and "jwt-session" in cookies.keys():
                            headers["Authorization"] = f"Bearer {cookies['jwt-session']}"
                        logging.info("Re-authentication succeeded; will retry download request.")
                        attempt += 1
                        time.sleep(1)
                        continue
                    except Exception as reauth_err:
                        logging.error(f"Re-authentication failed: {reauth_err}")
                        # fall through to raise below and trigger backoff
                else:
                    logging.error("No credentials provided; cannot re-authenticate after 403")
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
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    progress_bar.update(len(chunk))
                    file.write(chunk)
            progress_bar.close()
            is_successful = True
        except Exception as err:
            logging.error(f"Attempt {attempt} failed: {err}")
            time.sleep(timeout)
            timeout += 120
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

    if repo_url is None:
        raise ValueError("Repository URL is None.")

    if not repo_url.endswith('/'):
        repo_url = f'{repo_url}/'

    url = f'{repo_url}{filename}'
    logging.info(f'Uploading image {file_path} as raw to {url}')
    response = None
    try:
        with open(file_path, 'rb') as f:
            response = requests.put(url, auth=(username, password), data=f, verify=VERIFY_SSL)
    except Exception as err:
        logging.error(f"Failed to upload image: {err}")

    if response is not None and (response.status_code == 200 or response.status_code == 201):
        logging.info('File uploaded successfully')
        is_successful = True
    else:
        # Build helpful diagnostic information and a curl reproduction command for manual testing
        try:
            status = getattr(response, 'status_code', None)
            resp_text = None
            try:
                resp_text = response.text
            except Exception:
                resp_text = '<unable to read response.text>'
            preview = resp_text if resp_text is None or len(resp_text) <= 2000 else resp_text[:2000] + '...[truncated]'
            logging.error('Failed to upload file to %s. status=%s, preview=%s', url, status, preview)

            # Construct a curl command for the original PUT attempt (safe: do NOT include password)
            curl_put = (
                f"curl -v -u '{username}:REPLACE_WITH_PASSWORD' -H 'Content-Type: application/octet-stream' "
                f"--upload-file '{file_path}' '{url}'"
            )
            if not VERIFY_SSL:
                curl_put += ' --insecure'
            logging.error('To reproduce the PUT upload manually (replace REPLACE_WITH_PASSWORD):\n%s', curl_put)

            # If the repo URL looks like it contains a repository name, suggest a Nexus REST API curl as well
            try:
                parsed = urlparse(repo_url)
                path_parts = [p for p in parsed.path.split('/') if p]
                if len(path_parts) >= 1:
                    repo_name = path_parts[0]
                    raw_dir = '/'.join(path_parts[1:]) if len(path_parts) > 1 else ''
                    rest_api_url = f"{parsed.scheme}://{parsed.netloc}/service/rest/v1/components?repository={repo_name}"
                    curl_rest = (
                        f"curl -v -u '{username}:REPLACE_WITH_PASSWORD' \\"
                        f"-F raw.asset1=@'{file_path}';filename={filename} ")
                    if raw_dir:
                        curl_rest += f"-F raw.directory={raw_dir} "
                    curl_rest += f"-F raw.asset1.filename={filename} -F Filename={filename} '{rest_api_url}'"
                    if not VERIFY_SSL:
                        curl_rest += ' --insecure'
                    logging.error('If the server requires Nexus REST API upload, try:\n%s', curl_rest)
            except Exception:
                # best-effort; don't fail logging
                pass
        except Exception as e:
            logging.exception('Error while preparing upload diagnostics: %s', e)

    return is_successful, url


def download_file(url, destination, connections: int = None):
    """
    Downloads a file from the given URL and saves it to the specified destination.

    If `aria2c` is available on PATH, spawn it with multiple connections to
    accelerate downloads. Otherwise fall back to a requests-based downloader
    using a pooled Session and retries.

    :param url: str - URL of the file to download.
    :param destination: str - Path where the downloaded file should be saved.
    :param connections: int - number of parallel connections for aria2c (optional)
    """
    # Prefer aria2c when available for multi-connection downloads
    aria2c = shutil.which('aria2c')
    if aria2c:
        try:
            dest_dir = os.path.dirname(destination) or '.'
            dest_name = os.path.basename(destination)
            os.makedirs(dest_dir, exist_ok=True)
            # Determine number of connections to use; default to 4 if not provided
            conns = int(connections) if connections and int(connections) > 0 else 4
            aria2_cmd = [aria2c, '-x', str(conns), '-s', str(conns), f'--max-connection-per-server={conns}',
                         '--dir', dest_dir, '--out', dest_name, '--continue=true', '--max-tries=5', '--retry-wait=5', url]
            logging.info('Downloading via aria2c: %s', ' '.join(aria2_cmd))
            res = subprocess.run(aria2_cmd, capture_output=True, text=True)
            if res.returncode == 0:
                logging.info('aria2c download succeeded: %s', destination)
                return
            else:
                logging.warning('aria2c failed (rc=%s). Falling back to requests. stderr: %s', res.returncode, res.stderr[:500])
        except Exception as e:
            logging.warning('aria2c invocation failed: %s. Falling back to requests.', e)

    # Fallback to requests-based downloader (connection-pooled session)
    global _HTTP_SESSION
    try:
        _HTTP_SESSION
    except NameError:
        _HTTP_SESSION = requests.Session()
        # Configure a Retry strategy: a few retries on idempotent errors/backoffs
        retries = Retry(total=5,
                        backoff_factor=1,
                        status_forcelist=(500, 502, 503, 504),
                        allowed_methods=frozenset(['GET', 'HEAD']))
        adapter = HTTPAdapter(max_retries=retries, pool_connections=20, pool_maxsize=100)
        _HTTP_SESSION.mount('http://', adapter)
        _HTTP_SESSION.mount('https://', adapter)

    # Use a larger chunk size for faster writes (reduce Python overhead)
    chunk_size = 64 * 1024  # 64 KiB

    # Increase connect and read timeout to be tolerant of slow servers / networks.
    with _HTTP_SESSION.get(url, stream=True, verify=VERIFY_SSL, timeout=(10, 600)) as response:
        if response.status_code == 200:
            file_size = int(response.headers.get('Content-Length', 0))
            # Use tqdm for progress reporting
            progress = tqdm(total=file_size, desc=f'Downloading {os.path.basename(url)}', unit='B', unit_scale=True, unit_divisor=1024)
            os.makedirs(os.path.dirname(destination) or '.', exist_ok=True)
            with open(destination, 'wb') as file:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if not chunk:
                        continue
                    file.write(chunk)
                    progress.update(len(chunk))
            progress.close()
        else:
            raise RuntimeError(f"Failed to download file. Status code: {response.status_code}")


def fetch_emulator_image_list(repository_url, repository_name="emulator-images"):
    """
    Get all available emulator images from the remote Nexus repository.

    Args:
        repository_url (str): Base URL of the Nexus assets API.
        repository_name (str): Name of the repository (e.g., 'emulator-images').

    Returns:
        list: A list of asset dictionaries from the Nexus repository.
    """
    assets = []
    continuation_token = None
    logging.info(f"Fetching emulator images from {repository_url} with repository name {repository_name}...")
    while True:
        params = {'repository': repository_name}
        if continuation_token:
            params['continuationToken'] = continuation_token
            logging.debug('Fetching page with token: %s', continuation_token)
        response = requests.get(repository_url, params=params, timeout=10)

        if response.status_code != 200:
            logging.error('Failed to fetch data: %s', response.status_code)
            logging.debug(response.text)
            break

        data = response.json()
        assets.extend(data.get('items', []))
        continuation_token = data.get('continuationToken')

        if not continuation_token:
            break

    return assets


def fetch_app_manifest(graphql_url, cookies, firmware_id, filename):
    """
    Fetches the Android app manifest for the given firmware id and md5 hash.

    :param graphql_url: str - fmd api url for graphql.
    :param cookies: str - cookies jar for requests.
    :param firmware_id: str - id of the firmware to fetch the Android apps from.
    :param md5: str - md5 hash of the Android app to fetch.

    :returns: dict - dictionary of the android manifest.

    """
    logging.info(f"Fetching app manifest for firmware id {firmware_id} and filename {filename}...")
    headers = {"X-CSRFToken": cookies["csrftoken"], "Referer": graphql_url}
    temp_obj = Template(FMD_APP_MANIFEST_QUERY_TEMPLATE)
    params = temp_obj.substitute(firmware_id=firmware_id, filename=filename)
    params = json.loads(params)
    logging.info(f"Parsed params: {params}")
    try:
        with requests.post(graphql_url,
                           cookies=cookies,
                           params=params,
                           headers=headers,
                           verify=VERIFY_SSL) as response:
            if response and response.status_code != 200:
                raise RuntimeError(f"Could not fetch app manifest. Status code: {response.status_code};"
                                   f"response: {response.text}")
            resp_dict = response.json()
            logging.info(f"APP Manifest Response - firmware id {firmware_id} and filename {filename}: {resp_dict}")
            if not resp_dict or "data" not in resp_dict or "android_app_list" not in resp_dict["data"] or \
                    not resp_dict["data"]["android_app_list"]:
                raise RuntimeError(f"Could not fetch app manifest - no data found for firmware id {firmware_id} and filename {filename}.")
            if len(resp_dict["data"]["android_app_list"]) > 1:
                raise RuntimeError(f"More than one app manifest found for firmware id {firmware_id} and filename {filename}.")
            if "androidManifestDict" not in resp_dict["data"]["android_app_list"][0]:
                raise RuntimeError(f"Could not fetch app manifest for firmware id {firmware_id} and filename {filename}.")
            android_manifest_str = resp_dict["data"]["android_app_list"][0]["androidManifestDict"]
            android_manifest_dict = json.loads(android_manifest_str)
            if not android_manifest_dict:
                raise RuntimeError("Could not fetch app manifest.")
        logging.info(f"Fetched app manifest for firmware id {firmware_id} and filename {filename}.")
    except Exception as e:
        logging.error(f"Error fetching manifest for {firmware_id}:{filename} - {e}")
        return None

    return android_manifest_dict

