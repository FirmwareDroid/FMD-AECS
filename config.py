import os
from pathlib import Path

AECS_ROOT_DIR = str(Path().absolute())
SCRIPT_ROOT_PATH = os.path.dirname(os.path.realpath(__file__))
AOSP_PACKAGES_APPS_PATH = "packages/apps/"
META_BUILD_FILENAME = "meta_build.txt"
TEMPLATE_FOLDER = "templates/12/"
BASE_PATH = "build/make/target/product/"
BASE_PRODUCT_FILE_NAME = "base_product.mk"
BASE_SYSTEM_FILE_NAME = "base_system.mk"
BASE_VENDOR_FILE_NAME = "base_vendor.mk"
BUILD_OUT_PATH = os.path.join(SCRIPT_ROOT_PATH, "out/")
IMAGE_ARTEFACTS_ARM64_PATH = "image_artefacts/arm64-v8a/"
IMAGE_ARTEFACTS_X86_64_PATH = "image_artefacts/x86_64/"
IMAGE_ARTEFACTS_PATH = "image_artefacts/"
AOSP_BUILD_OUT_SDK_ARM64_PATH = "out/target/product/emulator_arm64/"
AOSP_BUILD_OUT_SDK_x86_64_PATH = "out/host/linux-x86/sdk/sdk_phone_x86"
AOSP_EMU_ZIP_FILENAME = "sdk-repo-linux-system-images-eng.ubuntu.zip"
IMAGE_ARTEFACTS_X86_64_ABS_PATH = os.path.join(SCRIPT_ROOT_PATH, IMAGE_ARTEFACTS_X86_64_PATH)
IMAGE_ARTEFACTS_ABS_PATH = os.path.join(SCRIPT_ROOT_PATH, IMAGE_ARTEFACTS_PATH)
EMULATOR_DOCKERFILE_ABS_PATH = os.path.join(AECS_ROOT_DIR, "emulator/Dockerfile")
DOCKER_PLATFORM_X86_64 = "linux/amd64"
DOCKER_PLATFORM_ARM64 = "linux/arm64"
FMD_FIRMWARE_BUILD_FILES_DOWNLOAD_TEMPLATE = "${url}/download/android_app/build_files"
SUPPORTED_ARCHITECTURES = ["x86_64", "arm64"]
SUPPORTED_LUNCH_TARGETS = ["sdk_x86_64-userdebug", "sdk_phone_arm64"]

# String templates
FMD_GRAPHQL_URL_TEMPLATE = '${url}/graphql/'
FMD_AUTH_QUERY_TEMPLATE = '{"query": "query Auth ' \
                          '{tokenAuth(password: \\\"${password}\\\", username: \\\"${username}\\\") {token}}",' \
                          '"operationName": "Auth"}'
FMD_AECS_FIRMWARE_QUERY_TEMPLATE = '{"query": "query GetFirmwareIdList {aecs_firmware_id_list}",' \
                                   '"operationName": "GetFirmwareIdList"}'
FMD_CSRF_URL_TEMPLATE = "${url}/csrf/"
VERIFY_SSL = False  # You can suppress warnings with: export PYTHONWARNINGS="ignore:Unverified HTTPS request"
