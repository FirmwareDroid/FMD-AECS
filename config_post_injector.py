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
PARTITION_NAME_LIST = [
    "super", "system", "vendor", "product", "odm", "oem", "data", "system_ext", "userdata",
    "system_other", "my_product", "my_stock", "my_heytap", "my_region", "my_carrier",
    "my_bigball", "my_manifest", "my_company", "my_engineering",
    "prism", "optics", "omr", "mi_ext", "cust"
]
OEM_CUSTOM_PARTITION_MAP = {
    "super": "system",
    "system_other": "system",
    "odm": "vendor/odm",
    "oem": "vendor/oem",
    "my_product": "product",
    "my_stock": "product",
    "my_heytap": "product",
    "my_region": "product",
    "my_carrier": "product",
    "my_bigball": "product",
    "my_manifest": "product",
    "my_company": "product",
    "my_engineering": "product",
    "prism": "system/prism",
    "optics": "system/optics",
    "omr": "system/omr",
    "mi_ext": "system_ext",
    "cust": "product",
}
MODULE_TYPE_ABI_COMPATIBLE = ["SHARED_LIBRARIES", "EXECUTABLES", "ETC", "MISC"]
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

PROTECTED_AOSP_PACKAGES_LIST = [
    "com.android.providers.settings",
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
    "com.android.ext.services",
    "com.google.android.ext.services",
    "com.android.emulator.multidisplay",
    "com.android.shell",
]

DEFAULT_SHARED_USER_ID_MAPPING_DICT = {
    "platform": [
        "android.uid.system",
        "android.uid.systemui",
        "android.uid.phone",
        "android.uid.nfc",
        "android.uid.se",
        "android.uid.shell",
        "android.uid.keychain",
        "android.uid.backup",
    ],
    "media": [
        "android.media",
        "android.uid.media",
    ],
    "shared": [
        "android.uid.shared",
        "android.uid.calendar",
    ],
    "networkstack": [
        "android.uid.networkstack",
        "android.uid.networkstack.mainnet",
    ],
    "bluetooth": [
        "android.uid.bluetooth",
    ],
}

COUPLED_CORE_GROUPS = {
    "zygote": [
        "system/framework/framework.jar",
        "system/framework/ext.jar",
        "system/lib64/libandroid_runtime.so",
        "system/lib/libandroid_runtime.so",
        "system/bin/app_process64",
        "system/bin/app_process32",
    ],
    "system_server": [
        "system/framework/services.jar",
        "system/lib64/libandroid_servers.so",
    ],
}

SAFE_VENDOR_PROP_PREFIXES = (
    "ro.miui.",
    "ro.oplus.",
    "ro.build.version.oplusrom",
    "ro.build.version.oneui",
    "ro.build.version.emui",
    "ro.build.version.magic",
    "ro.vivo.",
    "ro.Realme.",
    "ro.heytap.",
    "ro.coloros.",
    "ro.samsung.",
    "ro.sec.",
    "ro.sem.",
    "ro.config.",
    "ro.product.brand",
    "ro.product.manufacturer",
    "ro.product.model",
    "ro.product.name",
    "ro.product.device",
    "persist.sys.oplus.",
    "persist.sys.miui.",
)

UNSAFE_VENDOR_PROP_PREFIXES = (
    "ro.boot.",
    "ro.hardware.",
    "ro.board.platform",
    "ro.sf.lcd_density",
    "ro.opengles.",
    "ro.soc.",
    "ro.treble.",
    "ro.control_privapp_permissions",
    "ro.dalvik.vm.",
    "ro.zygote",
    "ro.Apex.",
    "ro.apex.",
    "ro.crypto.",
    "ro.vndk.",
)

EMULATOR_PROTECTED_FILES = [
    # Bionic linker, C/C++ runtime, and core IPC libraries
    "ld-android.so",
    "linker",
    "linker64",
    "linker_asan",
    "linker_asan64",
    "linker.config.pb",
    "libc.so",
    "libdl.so",
    "libdl_android.so",
    "libm.so",
    "libc++.so",
    "libcutils.so",
    "libvndksupport.so",
    "libbinder.so",
    "libbinder_ndk.so",
    "libhwbinder.so",
    # Goldfish / Ranchu EGL, GLES, and Vulkan loader libraries
    "libEGL.so",
    "libGLESv1_CM.so",
    "libGLESv2.so",
    "libGLESv3.so",
    "libvulkan.so",
    "libOpenglSystemCommon.so",
    "libGOLDFISH_AddressSpace.so",
    # Core Android / Emulator native daemons
    "servicemanager",
    "hwservicemanager",
    "vndservicemanager",
    "surfaceflinger",
    "vold",
    "netd",
    "installd",
    "lmkd",
    "logd",
    "app_process32",
]

EMULATOR_PROTECTED_KEYWORDS = [
    "gralloc.goldfish",
    "gralloc.ranchu",
    "vulkan.ranchu",
    "hwcomposer.ranchu",
    "audio.primary.goldfish",
    "libgoldfish",
    "libranchu",
    "goldfish_address_space",
    "/bin/servicemanager",
    "/bin/hwservicemanager",
    "/bin/vndservicemanager",
    "/bin/surfaceflinger",
    "/bin/vold",
    "/bin/netd",
    "/bin/installd",
]

UNSAFE_HARDWARE_FEATURES = (
    "android.hardware.fingerprint",
    "android.hardware.biometrics.",
    "android.hardware.strongbox_keystore",
    "android.hardware.keystore.app_attest_key",
    "android.hardware.vulkan.",
    "android.hardware.uwb",
    "android.hardware.nfc",
    "android.hardware.telephony.euicc",
    "android.hardware.se.omapi.",
    "android.hardware.identity_credential",
    "android.hardware.weaver",
    "android.hardware.reboot_escrow",
)

PROTECTED_RRO_TARGET_PACKAGES = {
    "android",
    "com.android.systemui",
    "com.android.settings",
    "com.android.phone",
    "com.android.server.telecom",
    "com.android.providers.settings",
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
    "com.android.ManagedProvisioning",
}

