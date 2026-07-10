import os

from config import (BUILD_OUT_PATH,
                    FILE_CONTEXT_TEMPLATE_PATH,
                    ROOT_PATH,
                    TEMPLATE_FOLDER,
                    EXTRACTED_PACKAGES_PATH,
                    VENDOR_NAMES,
                    MODULE_BASE_INJECT_DIR,
                    TMP_PATH
                    )
MAIN_LOG_FILE_PATH = "/tmp/fmd/out/post_injector_main.log"
SEMANTIC_INJECTOR_LOG_FILE_PATH = "/tmp/fmd/out/post_injector_semantic_injector.log"
PRINT_ALL_LOGS = True
PRINT_ERROR_LOGS = True
FOLDER_NAME_OBJECTS = "obj"
FOLDER_NAME_EXECUTABLES = "EXECUTABLES"
FOLDER_NAME_JAVA_LIBRARIES = "JAVA_LIBRARIES"
FOLDER_NAME_ETC = "ETC"
PARTITION_NAME_LIST = ["super", "system", "vendor", "product", "odm", "oem", "data", "system_ext", "userdata"]
MODULE_TYPE_ABI_COMPATIBLE = ["SHARED_LIBRARIES", "EXECUTABLES", "ETC"]
NAME_EXECUTION_TIME_LOG = "results_post_build_injector_metrics.json"
NAME_EXECUTION_MAPPING_TIME_LOG = "results_injection_function_performance_metrics.json"
SKIPPED_ERROR_LIST_NAME = "results_skipped_files.json"
SKIPPED_ERROR_LIST_FILE_PATH = os.path.join(TMP_PATH, SKIPPED_ERROR_LIST_NAME)
PATH_EXECUTION_TIME_LOG = os.path.join(TMP_PATH, NAME_EXECUTION_TIME_LOG)
PATH_MAPPING_EXECUTION_TIME_LOG = os.path.join(TMP_PATH, NAME_EXECUTION_MAPPING_TIME_LOG)
ENABLE_INJECTION_PERFORMANCE_LOG = False
PROPERTY_MERGE_CONFLICT_DIR = "property_merge_conflicts"
PATH_PROPERTY_MERGE_CONFLICTS_DIR = os.path.join(BUILD_OUT_PATH, PROPERTY_MERGE_CONFLICT_DIR)
# When True, ACVTool's instrumented APK files will be deleted from the ACV output folder
# before creating the archive so they are not included in the uploaded zip.
ACVTOOL_DELETE_INSTRUMENTED_APKS = True
FILE_MAP_PATH = "/tmp/fmd/out/file_map"