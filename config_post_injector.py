import os

from config import (AOSP_DEFAULT_PACKAGE_NAMES,
                    VENDOR_BLACKLISTED_PACKAGES,
                    BUILD_OUT_PATH,
                    FILE_CONTEXT_TEMPLATE_PATH,
                    ROOT_PATH,
                    TEMPLATE_FOLDER,
                    SHARED_USER_ID_MAPPING_DICT,
                    EXTRACTED_PACKAGES_PATH
                    )
PRINT_ALL_LOGS = True
PRINT_ERROR_LOGS = True
FOLDER_NAME_OBJECTS = "obj"
FOLDER_NAME_EXECUTABLES = "EXECUTABLES"
FOLDER_NAME_JAVA_LIBRARIES = "JAVA_LIBRARIES"
FOLDER_NAME_ETC = "ETC"
PARTITION_NAME_LIST = ["super", "system", "vendor", "product", "odm", "oem", "data"]
MODULE_TYPE_ABI_COMPATIBLE = ["SHARED_LIBRARIES", "EXECUTABLES", "ETC"]
OVERWRITE_APP_PROCESS_32 = True
NAME_EXECUTION_TIME_LOG = "results_post_build_injector_metrics.json"
PATH_EXECUTION_TIME_LOG = os.path.join(BUILD_OUT_PATH, NAME_EXECUTION_TIME_LOG)
CHECK_VNDK_VERSION_MISMATCH = True
# Singleton Apps: StorageManagerGoogle.apk
SKIPPED_APP_LIST = [
                    #"GooglePermissionController.apk",
                    #"GooglePackageInstaller.apk"
                    ]
for blacklisted_module_name in VENDOR_BLACKLISTED_PACKAGES:
    SKIPPED_APP_LIST.append(f"{blacklisted_module_name}.apk")

#".bprof",
#".policy"

SKIPPED_FILE_EXTENSION_LIST_GENERAL = [
                               #".rc", # Init files might break the emulator
                               ".ko", # Kernel modules break the emulator
                               ".prop", # Build properties should not be overwritten
                               #".apex", # Manual inject
                               #".capex", # Manual inject
                               #".prof",
                               ".original_apex", # Leftover from apex repacking
                               ".idsig", # Leftover from signing
                               ".art",
                               ".oat",
                               #".odex",
                               ".apex_original", # Leftover from the file extraction process
                               ".capex_original", # Leftover from the file extraction process
                               ".imgxtract", # Leftover from the file extraction process
                               ".extfextract" # Leftover from the file extraction process
                               ".raw",  # Leftover from the file extraction process
                               #".vdex",
                               ]

SKIPPED_FILE_EXTENSION_LIST_INDIRECT_INJECTION = [".rc"]



SKIPPED_FILE_ENDING_LIST = [
        "adbd_compressed.apex",
        "adbd.apex",
        "adbd_trimmed_compressed.apex",
        "adbd_trimmed.apex"
    ]


SKIPPED_BINARY_LIST = [
                        #"vndservicemanager", # problematic
                        "hwservicemanager", # problematic
                        "servicemanager", # problematic
                        "vold", # problematic
                        "vold_prepare_subdirs",
                        "vdc",
                        "flags_health_check",
                        "bpfloader",
                        #"libhardware.so",
                        #"libhardware_legacy.so",
                        "keystore2",
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
                        "init.rc",
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
                        ".product.img.raw", # Leftover from the file extraction process
                        ".system.img.raw",  # Leftover from the file extraction process
                        ".vendor.img.raw", # Leftover from the file extraction process
                        ".odm.img.raw", # Leftover from the file extraction process
                        ".oem.img.raw", # Leftover from the file extraction process
                        ".data.img.raw", # Leftover from the file extraction process
                        ".super.img.raw", # Leftover from the file extraction process
                        "product.img.raw", # Leftover from the file extraction process
                        "system.img.raw", # Leftover from the file extraction process
                        "vendor.img.raw", # Leftover from the file extraction process
                        "odm.img.raw", # Leftover from the file extraction process
                        "oem.img.raw", # Leftover from the file extraction process
                        "data.img.raw", # Leftover from the file extraction process
                        "super.img.raw", # Leftover from the file extraction process
                        "libgatekeeper.so",
                        "apex_pubkey",
                        "apex_manifest.json",
                        "apex_payload.img",
                        "apex_manifest.pb",
                        "apex_build_info.pb",
                        "AndroidManifest.xml",
                        "libbinder.so",
                        "libc.so",
                        "libbase.so",
                        "libcutils.so",
                        "liblogwrap.so",
                        "libselinux.so",
                        "libutils.so",
                        "libc++.so",
                        "libm.so",
                        "libdl.so",
                        "liblog.so",
                        "libselinux.so",
                        "libbinder_ndk.so",
                        "libc.so",
                        "android.hardware.boot@1.0.so",
                        "libbase.so",
                        "libcrypto.so",
                        "libcrypto_utils.so",
                        "libdiskconfig.so",
                        "libext4_utils.so",
                        "libf2fs_sparseblock.so",
                        "libgsi.so",
                        "libhardware.so",
                        "libhardware_legacy.so",
                        "libincfs.so",
                        "libhidlbase.so",
                        "libkeyutils.so",
                        "liblogwrap.so",
                        "libsysutils.so",
                        "android.hardware.health.storage@1.0.so",
                        "android.hardware.health.storage-V1-ndk_platform.so",
                        "android.system.keystore2-V1-ndk_platform.so",
                        "android.security.maintenance-ndk_platform.so",
                        "libkeymint_support.so",
                        "libc++.so",
                        "libhwbinder.so",
                        "libfs_mgr_binder.so", #
                        "libbinderdebug.so",
                        "libbinderwrapper.so",
                        "libbrillo-binder.so",
                        "libbinder_ndk.so",
                        "liblog.so",
                        "libc.so",
                        "libselinux.so",
                        "libkeystore2_aaid.so",
                        "libkeystore2_apc_compat.so",
                        "libkeystore2_crypto.so",
                        "libcrypto.so",
                        "libkm_compat_service.so",
                        "libkeystore2_vintf_cpp.so",
                        "libsqlite.so",
                        "android.security.apc-ndk_platform.so",
                        "android.system.keystore2-V1-ndk_platform.so",
                        "libchrome.so",
                        "libcrypto.so",
                        "libprotobuf-cpp-lite.so",
                        "libgui.so", # Breaks the build process in case the binary is not compatible with Stagefright after post-injection
                        "libfs_mgr.so", # Breaks INIT process
                        "libandroid_runtime.so",
                        "libnativeloader.so",
                        "libsigchain.so",
                        "libwilhelm.so",
                        "libsurfaceflinger.so",
                        "rfsd",
                        "cbd",
                        "gpuservice",
                        "libgpuservice.so",
                        "gpuservice.rc,"
                        "rild_exynos",
                        "netd",
                       ]











# "libminijail.so",
# "libavservices_minijail_vendor.so",
# "boot-framework.art",
# "boot-core-icu4j.art",


# "app_process", # Basically zygote64
# "seccomp", # BPF filters might break the emulator
# "hardware",
# "android.hidl",
# "vendor.qti.hardware",
# "qti",
# All files that contain these keywords will be skipped and not injected in the post-injector.
# Try to avoid using keywords that are too generic, as they might skip files that should be injected.
SKIPPED_KEYWORD_LIST = ["keystore",
                        #"binder",
                        #"vndk",
                        "keymaster",
                        "selinux",
                        "android.system.suspend",
                        "android.hardware",
                        "vintf",
                        "recovery-refresh",
                        "vendor.sensors",
                        "atrace",
                        "qseecom",
                        "exfat",
                        "secureboot",
                        "_apex",         # Skipped all files from extracted apex folder
                        "_capex",         # Skipped all files from extracted capex folder
                        ]

# When sets allows all files to be overwritten, ignoring the ALLOW_FILE_OVERWRITE
ALLOW_ALL_FILE_OVERWRITE = True
# "services.jar.bprof",
# "framework.jar"
ALLOW_FILE_OVERWRITE = []

# "dex2oat"
SKIPPED_APEX_KEYWORD_LIST = ["adb"]

ALLOWED_FILE_OVERWRITE_EXTENSION_LIST = [".ogg",
                                         ".otf",
                                         ".ttf"
                                         ]


ALLOW_FILE_OVERWRITE_EXTENSIONS = [".jar"]


#for default_module_name in AOSP_DEFAULT_PACKAGE_NAMES:
#    ALLOW_FILE_OVERWRITE.append(f"{default_module_name}.apk")


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
# "com.google.android.adbd.apex", --> Blocks adb access
# APEX Files in this list will be merged in the POST-INJECTOR -> APEX file not in this list will be repackaged
# in the pre-injector.
ALLOW_FILE_INJECT_ALWAYS = [
                            "vndservicemanager.rc",
                            "init.zygote64_32",
                            "init.zygote32.rc"
                            "audioserver.rc",
                            "cameraserver.rc"
                            ]

ALLOW_APEX_MERGE_KEYWORD_LIST = [
                            "sdkext",
                            "extservices",
                            "extservice",
                            "wifi",
                            "tethering",
                            "i18n",
                            "vndk",
                            "ipsec",
                            "scheduling",
                            "statsd",
                            "resolv",
                            "neuralnetworks",
                            "mediaprovider",
                            "permission",
                            "runtime",
                            "art",
                            "conscrypt",
                            "appsearch",
                            "swcodec",
                            "media",
                            "tzdata",
                            "tzdata3",
                        ]


ALLOW_APEX_INJECTION_MERGE = True
INJECT_APEX_VENDOR_FILES = True
INJECT_APEX_VENDOR_APPS = True
ALLOW_MIXED_APEX_FILES = False  # IF False: Only Vendor files are used and no emulator files
REMOVE_APEX_APK_FILE = True
REPLACE_AVB_KEYS = False

APEX_DEFAULT_PATHS_DICT = {
    "sdkext": "packages/modules/SdkExtensions",
    "extservices": "packages/modules/ExtServices/apex",
    "wifi": "packages/modules/Wifi/apex",
    "tethering": "packages/modules/Connectivity/Tethering/apex",
    "i18n": "packages/modules/RuntimeI18n/apex",
    "vndk": "packages/modules/vndk/apex",
    "ipsec": "packages/modules/IPsec/apex",
    "scheduling": "packages/modules/Scheduling/apex",
    "adb": "packages/modules/adb/apex",
    "statsd": "packages/modules/StatsD/apex", #
    "resolv": "packages/modules/DnsResolver/apex", #
    "neuralnetworks": "packages/modules/NeuralNetworks/apex",
    "cellbroadcast": "packages/apps/CellBroadcastReceiver/apex",
    "mediaprovider": "packages/providers/MediaProvider/apex",
    "telephony": "packages/services/Telephony/apex",
    "permission": "packages/modules/Permission",
    "runtime":"bionic/apex/",
    "art": "art/build/apex",
    "conscrypt":"external/conscrypt/apex",
    "appsearch":"frameworks/base/apex/appsearch/",
    "swcodec":"frameworks/av/apex",
    "media": "frameworks/av/apex",
    "tzdata": "system/timezone/apex/",
    "tzdata3": "system/timezone/apex/",
}



APEX_DEFAULT_EMULATOR_PATHS_DICT = {
    "appsearch": "com.android.appsearch",
    "conscrypt": "com.android.conscrypt",
    "i18n": "com.android.i18n",
    "media": "com.android.media",
    "mediaprovider": "com.android.mediaprovider",
    "statsd": "com.android.os.statsd",
    "resolv": "com.android.resolv",
    "scheduling": "com.android.scheduling",
    "tethering": "com.android.tethering",
    "vndk": "com.android.vndk.v32",
    "adb": "com.android.adbd",
    "art": "com.android.art",
    "extservices": "com.android.extservices",
    "ipsec": "com.android.ipsec",
    "swcodec": "com.android.media.swcodec",
    "neuralnetworks": "com.android.neuralnetworks",
    "permission": "com.android.permission",
    "runtime": "com.android.runtime",
    "sdkext": "com.android.sdkext",
    "tzdata": "com.android.tzdata",
    "wifi": "com.android.wifi",
}




LIST_SINGLETON_APPS = [
    "PackageInstaller.apk",          # com.android.packageinstaller
    "SystemUI.apk",                  # com.android.systemui
    "Settings.apk",                  # com.android.settings
    "Telecom.apk",                   # com.android.server.telecom
    "ContactsProvider.apk",          # com.android.providers.contacts
    "MediaProvider.apk",             # com.android.providers.media
    "PermissionController.apk",      # com.android.permissioncontroller
    "LatinIME.apk",                  # com.android.inputmethod.latin
    "Dialer.apk",                    # com.android.dialer
    "NetworkStack.apk",              # com.android.networkstack -> Removing works
    "DocumentsUI.apk",               # com.android.documentsui
    "Provision.apk",                 # com.android.provision
    #"MediaProvider.apx"
    #"CellBroadcastApp.apk",          #  com.android.cellbroadcast
    #"CellBroadcastServiceModule.apk", # com.android.cellbroadcast
    #"OsuLogin.apk",                  # com.android.hotspot2.osulogin
    #"ServiceWifiResources.apk",      # com.android.wifi.resources
    #"Tethering.apk",
    #"ServiceConnectivityResources.apk"
    #"Launcher3.apk",                 # com.android.launcher3
    #"Phone.apk",                     # com.android.phone
    #"TeleService.apk"                # com.android.phone.IccProvider
    #"DownloadProvider.apk",          # com.android.providers.downloads
    #"Messaging.apk",                 # com.android.messaging
    #"BackupTransport.apk",           # com.android.backuptransport
    #"Updater.apk",                   # com.android.updater
    #"DevicePolicyManager.apk",       # com.android.devicepolicy
    #"PlayServices.apk",              # com.google.android.gms
    #"WebView.apk"                    # com.android.webview
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



INDIRECT_INJECTION_FILE_MAPPING = {
    "framework.jar": "obj/JAVA_LIBRARIES/framework-minus-apex_intermediates/javalib.jar",
    "com.google.android.tzdata3.apex": "obj/ETC/com.android.tzdata_intermediates/com.android.tzdata.apex",
    "framework-res.apk": "obj/APPS/framework-res_intermediates/package.apk",
    #"services.jar": "obj/JAVA_LIBRARIES/services_intermediates/services_intermediates",
}














