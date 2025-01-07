from config import (AOSP_DEFAULT_PACKAGE_NAMES,
                    VENDOR_BLACKLISTED_PACKAGES,
                    SHARED_USER_ID_MAPPING_DICT,
                    ROOT_PATH,
                    TEMPLATE_FOLDER,
                    EXTRACTED_PACKAGES_PATH,
                    FILE_CONTEXT_TEMPLATE_PATH)

FOLDER_NAME_OBJECTS = "obj"
FOLDER_NAME_EXECUTABLES = "EXECUTABLES"
FOLDER_NAME_JAVA_LIBRARIES = "JAVA_LIBRARIES"
FOLDER_NAME_ETC = "ETC"
PARTITION_NAME_LIST = ["super", "system", "vendor", "product", "odm", "oem", "data"]
MODULE_TYPE_ABI_COMPATIBLE = ["SHARED_LIBRARIES", "EXECUTABLES", "ETC"]

# Singleton Apps: StorageManagerGoogle.apk
SKIPPED_APP_LIST = ["GooglePermissionController.apk", "GooglePackageInstaller.apk"]
for blacklisted_module_name in VENDOR_BLACKLISTED_PACKAGES:
    SKIPPED_APP_LIST.append(f"{blacklisted_module_name}.apk")

#".bprof",
#".policy",
SKIPPED_FILE_EXTENSION_LIST = [
                               ".rc",
                               ".ko",
                               ".prop",
                               ".capex",
                               ".prof",
                               ".apex",
                               ".original_apex", # Leftover from apex repacking
                               ".idsig", # Leftover from signing
                               ".art",
                               ".oat",
                               ".odex",
                               ".vdex",
                               ]

# "vndservicemanager", # problematic
SKIPPED_BINARY_LIST = [
                       "hwservicemanager", # problematic
                       "servicemanager", # problematic
                       "vold",
                       "keystore2",
                       "vdc",
                       "console",
                       "zygote",
                       "tee",
                       "qemu-props",
                       "ueventd",
                       "wait_for_keymaster",
                       "bootstat",
                       "wpa_supplicant",
                       "apexd-bootstrap",
                       "bootstrap",
                       "fsverity_init",
                       "init",
                       "apexd",
                       "atrace",
                       "setprop",
                       "getprop",
                       "std.build.prop",
                       "pro.build.prop",
                       "default.prop",
                       "lmkd",
                       "build.prop",
                       "otacerts.zip",  # Allow to overwrite with own certificates
                       "raw.image",  # Leftover from the file extraction process
                       ".product.img.raw",
                       ".system.img.raw",
                       ".vendor.img.raw",
                       ".odm.img.raw",
                       ".oem.img.raw",
                       ".data.img.raw",
                       ".super.img.raw",
                       "libgatekeeper.so",
                       ]
# "libminijail.so",
# "libavservices_minijail_vendor.so",
# "boot-framework.art",
# "boot-core-icu4j.art",


# "app_process", # Basically zygote64
# "seccomp", # BPF filters might break the emulator
SKIPPED_KEYWORD_LIST = ["keystore",
                        "keymaster",
                        "selinux",
                        "android.hardware",
                        "hardware",
                        "android.hidl",
                        "hwservicemanager",
                        "vintf",
                        "vndk",
                        "vold",
                        "recovery-refresh",
                        "vendor.sensors",
                        "atrace",
                        "qseecom",
                        "exfat",
                        "vendor.qti.hardware",
                        "qti",
                        "secureboot",
                        ]

# "dex2oat"
SKIPPED_APEX_KEYWORD_LIST = []


ALLOWED_FILE_OVERWRITE_EXTENSION_LIST = [".ogg",
                                         ".otf",
                                         ".ttf"
                                         ]

ALLOW_FILE_OVERWRITE = ["framework-res.apk",
                        "framework-ext-res.apk",
                        "passwd",
                        "group",
                        "com.google.android.hardwareinfo.xml",
                        "services.jar.bprof"
                        ]

for default_module_name in AOSP_DEFAULT_PACKAGE_NAMES:
    ALLOW_FILE_OVERWRITE.append(f"{default_module_name}.apk")

ALLOWED_KEYWORD = ["Overlay",
                   "Connectivity",
                   "Wifi",
                   "Telephony",
                   "Telecom",
                   "TeleService",
                   "TelephonyProvider",
                   "NetworkStackGoogle",
                   "SystemUI",  # Breaks SystemUI
                   "libadb_protos"
                   ]
#for blacklisted_keyword in BLACKLISTED_KEYWORDS:
#    ALLOWED_KEYWORD.append(blacklisted_keyword)

#, --> No exact file match com.android.tzdata.apex is used
# "com.google.android.tzdata3.apex", --> Conflict with com.google.android.tzdata.apex
# "com.google.android.adbd.apex", --> Blocks adb access
# "init.zygote32.rc",
# "init.zygote64.rc",
# "init.zygote64_32.rc",
# "boot-framework.art",


#"com.google.pixel.camera.hal.apex", > Skipped is added via pre-injector
# "com.google.android.telephony.apex", -> Skipped is added via pre-injector
# "com.google.mainline.primary.libs.apex", -> Skipped is added via pre-injector

# "com.android.apex.cts.shim.apex", ->  Not important
# "com.android.vndk.current.apex",  # No Injection: Problematic updates many com.android.hardware libraries
ALLOW_FILE_INJECT_ALWAYS = ["installd.rc",
                            "com.google.android.tethering.apex",
                            "com.android.i18n.apex",
                            "com.google.android.extservices.apex",
                            "com.google.android.conscrypt.apex",
                            "com.google.android.wifi.apex",
                            "com.google.android.cellbroadcast.apex",
                            "com.google.android.mediaprovider.apex",
                            "com.google.android.sdkext.apex",
                            "com.google.android.ipsec.apex",
                            "com.google.android.resolv.apex",
                            "com.google.android.os.statsd.apex",
                            "com.android.runtime.apex",  # Problematic -> Without boot-framework error? Root cause of file not found error?
                            "com.google.android.art.apex",  # Problematic, contains boot.art, boot.oat, and boot.vdex
                            "com.google.android.media.swcodec.apex",
                            "com.google.android.scheduling.apex",
                            "com.google.android.appsearch.apex",
                            "com.google.android.neuralnetworks.apex",
                            "com.google.android.media.apex",   # Seccomp filter breaks goldfish media service? (emulator) -> Workaround change seccompfilter
                            "com.google.android.permission.apex",
                            "com.google.android.hardwareinfo.xml",
                            "vndservicemanager.rc",
                            ]

ALLOW_APEX_FILE_INJECT = ["derive_classpath.rc",
                          "derive_sdk.rc",
                          "libminijail.so",
                          "libavservices_minijail_vendor.so",
                          "mediaextractor.policy",
                          "mediaswcodec.policy"
                          ]

ALLOW_FILE_INJECT_ALWAYS_KEYWORD_LIST = []
ALLOW_APEX_FILE_INJECT_ALWAYS_KEYWORD_LIST = [".txt", ".art"]
DISALLOW_APEX_FILE_OVERWRITE = []

COPY_TO_SPECIFIC_PATH = {
    "boot-framework.art": "./system/framework/"
}

# Files that need to be adjusted for the emulator
FILES_TO_MODIFY = ["systemserverclasspath.pb", "bootclasspath.pb"]