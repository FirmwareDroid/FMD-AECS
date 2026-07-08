import os
import shutil
import tempfile
import argparse
import logging
from urllib.parse import urlparse
import requests

# Assuming this comes from your existing configuration setup
# Replace this with your actual import if config.py is in the same directory:
# from config import VERIFY_SSL
VERIFY_SSL = False

# Setup basic logging to see output in the console
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def upload_image_as_raw(repo_url, username, password, file_path, filename):
    """
    Uploads an image as raw to the given repository.

    :param repo_url: str - URL of the repository.
    :param username: str - Username to authenticate to the repository.
    :param password: str - Password to authenticate to the repository.
    :param file_path: str - Path to the image file.
    :param filename: str - Name of the file to upload.

    :return: (bool, str) - True if the upload was successful, False otherwise, along with the URL.
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

    if response is not None and (response.status_code in [200, 201]):
        logging.info('File uploaded successfully')
        is_successful = True
    else:
        try:
            status = getattr(response, 'status_code', None)
            resp_text = None
            try:
                resp_text = response.text
            except Exception:
                resp_text = '<unable to read response.text>'
            preview = resp_text if resp_text is None or len(resp_text) <= 2000 else resp_text[:2000] + '...[truncated]'
            logging.error('Failed to upload file to %s. status=%s, preview=%s', url, status, preview)

            curl_put = (
                f"curl -v -u '{username}:REPLACE_WITH_PASSWORD' -H 'Content-Type: application/octet-stream' "
                f"--upload-file '{file_path}' '{url}'"
            )
            if not VERIFY_SSL:
                curl_put += ' --insecure'
            logging.error('To reproduce the PUT upload manually (replace REPLACE_WITH_PASSWORD):\n%s', curl_put)

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
                pass
        except Exception as e:
            logging.exception('Error while preparing upload diagnostics: %s', e)

    return is_successful, url


def main():
    parser = argparse.ArgumentParser(description="Zip and upload target folders from emulator output.")
    parser.add_argument('--input-path', default='./emulator_out',
                        help="Path to emulator output directory (default: ./emulator_out)")
    parser.add_argument('--url', required=True, help="Repository destination URL")
    parser.add_argument('--user', required=True, help="Repository username")
    parser.add_argument('--password', required=True, help="Repository password")
    parser.add_argument('--output-file', default='uploaded_urls.txt',
                        help="File to write successful target URLs to (default: uploaded_urls.txt)")
    parser.add_argument('--target-folder', default='acv_snaps',
                        help="The folder name inside the firmware directory to zip up (default: acv_snaps)")
    parser.add_argument('--prefix', default='acvtool_snaps_',
                        help="Prefix for the uploaded zip filename (default: acvtool_snaps_)")

    args = parser.parse_args()

    if not os.path.exists(args.input_path):
        logging.error(f"Input path does not exist: {args.input_path}")
        return

    successful_urls = []

    # Walk through the structure: input_path/<firmware_id>/<target_folder>
    for firmware_id in os.listdir(args.input_path):
        firmware_dir = os.path.join(args.input_path, firmware_id)

        if os.path.isdir(firmware_dir):
            target_dir = os.path.join(firmware_dir, args.target_folder)

            if os.path.isdir(target_dir):
                zip_filename = f"{args.prefix}{firmware_id}.zip"
                logging.info(f"Processing '{args.target_folder}' for firmware: {firmware_id}")

                with tempfile.TemporaryDirectory() as tmpdir:
                    zip_base_path = os.path.join(tmpdir, f"{args.prefix}{firmware_id}")

                    logging.info(f"Zipping {target_dir}...")
                    archive_path = shutil.make_archive(zip_base_path, 'zip', target_dir)

                    success, target_url = upload_image_as_raw(
                        repo_url=args.url,
                        username=args.user,
                        password=args.password,
                        file_path=archive_path,
                        filename=zip_filename
                    )

                    if success:
                        logging.info(f"Successfully processed and uploaded {zip_filename}")
                        successful_urls.append(target_url)
                    else:
                        logging.error(f"Failed to upload {zip_filename}")

    # Write the tracking file if any uploads succeeded
    if successful_urls:
        try:
            with open(args.output_file, 'w') as f:
                for url in successful_urls:
                    f.write(f"{url}\n")
            logging.info(f"Wrote {len(successful_urls)} tracking URLs to manifest: {args.output_file}")
        except Exception as e:
            logging.error(f"Failed to save output URL file to {args.output_file}: {e}")
    else:
        logging.warning("No URLs saved because zero uploads succeeded.")


if __name__ == '__main__':
    main()