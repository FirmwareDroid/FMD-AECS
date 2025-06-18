import os

from config import (BUILD_OUT_PATH,
                    FILE_CONTEXT_TEMPLATE_PATH,
                    ROOT_PATH,
                    TEMPLATE_FOLDER,
                    EXTRACTED_PACKAGES_PATH,
                    VENDOR_NAMES,
                    MODULE_BASE_INJECT_DIR
                    )
PRINT_ALL_LOGS = True
PRINT_ERROR_LOGS = True
FOLDER_NAME_OBJECTS = "obj"
FOLDER_NAME_EXECUTABLES = "EXECUTABLES"
FOLDER_NAME_JAVA_LIBRARIES = "JAVA_LIBRARIES"
FOLDER_NAME_ETC = "ETC"
PARTITION_NAME_LIST = ["super", "system", "vendor", "product", "odm", "oem", "data"]
MODULE_TYPE_ABI_COMPATIBLE = ["SHARED_LIBRARIES", "EXECUTABLES", "ETC"]
NAME_EXECUTION_TIME_LOG = "results_post_build_injector_metrics.json"
PATH_EXECUTION_TIME_LOG = os.path.join(BUILD_OUT_PATH, NAME_EXECUTION_TIME_LOG)

