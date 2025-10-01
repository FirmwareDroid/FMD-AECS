import json
import os
import logging

ROOT_PATH = os.path.dirname(os.path.realpath(__file__))
AOSP_PACKAGES_APPS_PATH = "packages/apps/"
META_BUILD_SYSTEM_FILENAME = "meta_build_system.txt"
META_BUILD_VENDOR_FILENAME = "meta_build_vendor.txt"
META_BUILD_PRODUCT_FILENAME = "meta_build_product.txt"
META_BUILD_SYSTEM_EXT_FILENAME = "meta_build_system_ext.txt"
META_BUILD_HANDHELD_SYSTEM_FILENAME = "meta_handheld_system_ext.txt"
META_BUILD_HANDHELD_SYSTEM_EXT_FILENAME = "meta_handheld_system.txt"
META_BUILD_HANDHELD_VENDOR_FILENAME = "meta_handheld_vendor.txt"
META_BUILD_HANDHELD_PRODUCT_FILENAME = "meta_handheld_product.txt"
META_BUILD_FILENAMES = [META_BUILD_SYSTEM_FILENAME,
                        META_BUILD_SYSTEM_EXT_FILENAME,
                        META_BUILD_VENDOR_FILENAME,
                        META_BUILD_PRODUCT_FILENAME,
                        META_BUILD_HANDHELD_SYSTEM_FILENAME,
                        META_BUILD_HANDHELD_SYSTEM_EXT_FILENAME,
                        META_BUILD_HANDHELD_VENDOR_FILENAME,
                        META_BUILD_HANDHELD_PRODUCT_FILENAME]
TEMPLATE_FOLDER = "templates/"
BASE_PATH = "build/make/target/product/"
BASE_PRODUCT_FILE_NAME = "base_product.mk"
BASE_SYSTEM_FILE_NAME = "base_system.mk"
BASE_VENDOR_FILE_NAME = "base_vendor.mk"
BASE_SYSTEM_EXT_FILE_NAME = "base_system_ext.mk"
BASE_HANDHELD_SYSTEM_FILE_NAME = "handheld_system.mk"
BASE_HANDHELD_SYSTEM_EXE_FILE_NAME = "handheld_system_ext.mk"
BASE_HANDHELD_PRODUCT_FILE_NAME = "handheld_product.mk"
BASE_HANDHELD_VENDOR_FILE_NAME = "handheld_vendor.mk"
BASE_FILENAMES = [BASE_PRODUCT_FILE_NAME,
                  BASE_SYSTEM_FILE_NAME,
                  BASE_SYSTEM_EXT_FILE_NAME,
                  BASE_VENDOR_FILE_NAME,
                  BASE_HANDHELD_SYSTEM_FILE_NAME,
                  BASE_HANDHELD_SYSTEM_EXE_FILE_NAME,
                  BASE_HANDHELD_PRODUCT_FILE_NAME,
                  BASE_HANDHELD_VENDOR_FILE_NAME]
BUILD_OUT_PATH = os.path.join(ROOT_PATH, "out/")
AOSP_BUILD_OUT_SDK_ARM64_PATH = "out/target/product/emulator_arm64/"
AOSP_BUILD_OUT_SDK_ARM64_x64_PATH = "out/target/product/emulator64_arm64/"
AOSP_BUILD_OUT_SDK_ARM64_x64_PATH_A14 = "out/target/product/emu64a/"
AOSP_BUILD_OUT_SDK_x86_64_PATH = "out/target/product/emulator_x86_64/"
AOSP_EMU_ZIP_FILENAME_A11 = f"sdk_phone_arm64-img-eng.{os.getlogin()}.zip"
AOSP_EMU_ZIP_FILENAME_A12_A13 = f"sdk-repo-linux-system-images-eng.{os.getlogin()}.zip"
AOSP_EMU_ZIP_FILENAME = f"sdk-repo-linux-system-images.zip"
NEXUS_SERVICE_ENDPOINT = "service/extdirect"
NEXUS_EMULATOR_REPOSITORY = "repository/emulator-images/"
NEXUS_DOCKER_EMULATOR_REPOSITORY = "repository/docker-emulator-images/"
DOCKER_PLATFORM_X86_64 = "linux/amd64"
DOCKER_PLATFORM_ARM64 = "linux/arm64"
FMD_FIRMWARE_BUILD_FILES_DOWNLOAD_TEMPLATE = "${url}/download/android_app/build_files"
SUPPORTED_ARCHITECTURES = ["x86_64", "arm64"]
SUPPORTED_LUNCH_TARGETS = ["sdk_phone_x86_64-userdebug",  # Android 11 / 12 / 13 "sdk_x86_64-userdebug"
                           "sdk_phone_arm64-userdebug",  # Android 12 -> Works
                           "sdk_phone64_arm64-userdebug",  # Android 13 "sdk_arm64-userdebug"
                           "sdk_phone64_arm64-ap2a-userdebug"
                           ]
NAME_BUILD_FILE_LOG = "results_build_times.json"
NAME_BUILD_INJECTOR_LOG = "results_build_injector.json"
PATH_BUILD_FILE_LOG = os.path.join(BUILD_OUT_PATH, NAME_BUILD_FILE_LOG)
PATH_BUILD_INJECTOR_LOG = os.path.join(BUILD_OUT_PATH, NAME_BUILD_INJECTOR_LOG)
FILE_CONTEXT_TEMPLATE_PATH = os.path.join(ROOT_PATH, TEMPLATE_FOLDER, "file_contexts")
APEX_PRIVATE_KEY_PATH = os.path.join(ROOT_PATH, TEMPLATE_FOLDER, "apex.key")
APEX_PUBKEY_PATH = os.path.join(ROOT_PATH, TEMPLATE_FOLDER, "apex.x509.pem")
BUILD_RETRY_COUNT = 1
FMD_GRAPHQL_URL_TEMPLATE = '${url}/graphql/'
FMD_AUTH_QUERY_TEMPLATE = '{"query": "query Auth ' \
                          '{tokenAuth(password: \\\"${password}\\\", username: \\\"${username}\\\") {token}}",' \
                          '"operationName": "Auth"}'
FMD_APP_MANIFEST_QUERY_TEMPLATE = '{"query": "query GetManifest ' \
                                   '{android_app_list(fieldFilter: {filename: \\\"${filename}\\\", firmware_id_reference: \\\"${firmware_id}\\\"}) {androidManifestDict}}",' \
                                    '"operationName": "GetManifest"}'

FMD_AECS_FIRMWARE_QUERY_TEMPLATE = ('{"query": "query GetFirmwareIdList '
                                    '{aecs_job_list {pk, arch, firmwareIdList { '
                                    'edges {'
                                    'node {'
                                    'id}'
                                    '}'
                                    '}'
                                    '}}",'
                                    '"operationName": "GetFirmwareIdList"}')
FMD_CSRF_URL_TEMPLATE = "${url}/csrf/"
PACKAGE_EXTRACTION_DIR_NAME = "extracted_packages"
EXTRACTION_ALL_FILES_DIR_NAME = "ALL_FILES"
EXTRACTED_PACKAGES_PATH = str(os.path.join(BUILD_OUT_PATH, PACKAGE_EXTRACTION_DIR_NAME))
VERIFY_SSL = False  # You can suppress warnings with: export PYTHONWARNINGS="ignore:Unverified HTTPS request"

MODULE_BASE_INJECT_DIR = "packages/modules/fmd/"

VENDOR_NAMES = [
    "Google", "Samsung", "Apple", "Huawei", "Xiaomi", "Oppo", "Vivo", "OnePlus",
    "Realme", "Sony", "LG", "Nokia", "Motorola", "Asus", "Lenovo", "ZTE", "HTC",
    "Honor", "Meizu", "BlackBerry", "Alcatel", "Micromax", "Infinix", "Tecno",
    "Lava", "Coolpad", "Panasonic", "Sharp", "LeEco", "Gionee", "Itel", "Karbonn",
    "Blu", "Wiko", "Fairphone", "Essential", "Pixel", "Miui"
]
SKIPPED_MODULE_NAMES = []
PRE_INJECTOR_CONFIG = {}
POST_INJECTOR_CONFIG = {}

PRE_INJECTOR_CONFIG_PATH = ""
POST_INJECTOR_CONFIG_PATH = ""