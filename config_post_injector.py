from config import (AOSP_DEFAULT_PACKAGE_NAMES,
                    VENDOR_BLACKLISTED_PACKAGES,
                    SHARED_USER_ID_MAPPING_DICT,
                    ROOT_PATH,
                    TEMPLATE_FOLDER,
                    EXTRACTED_PACKAGES_PATH,
                    FILE_CONTEXT_TEMPLATE_PATH)
PRINT_ALL_LOGS = True
PRINT_ERROR_LOGS = True
FOLDER_NAME_OBJECTS = "obj"
FOLDER_NAME_EXECUTABLES = "EXECUTABLES"
FOLDER_NAME_JAVA_LIBRARIES = "JAVA_LIBRARIES"
FOLDER_NAME_ETC = "ETC"
PARTITION_NAME_LIST = ["super", "system", "vendor", "product", "odm", "oem", "data"]
MODULE_TYPE_ABI_COMPATIBLE = ["SHARED_LIBRARIES", "EXECUTABLES", "ETC"]

# Singleton Apps: StorageManagerGoogle.apk
SKIPPED_APP_LIST = [
                    #"GooglePermissionController.apk",
                    #"GooglePackageInstaller.apk"
                    ]
for blacklisted_module_name in VENDOR_BLACKLISTED_PACKAGES:
    SKIPPED_APP_LIST.append(f"{blacklisted_module_name}.apk")

#".bprof",
#".policy"

SKIPPED_FILE_EXTENSION_LIST = [
                               ".rc", # Init files might break the emulator
                               ".ko", # Kernel modules break the emulator
                               ".prop", # Build properties should not be overwritten
                               ".apex", # Manual inject
                               ".capex", # Manual inject
                               #".prof",
                               ".original_apex", # Leftover from apex repacking
                               ".idsig", # Leftover from signing
                               ".art",
                               ".oat",
                               ".odex",
                               #".vdex",
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
                       "apex_pubkey",
                       "apex_manifest.json",
                       "apex_payload.img",
                       "apex_manifest.pb",
                       "apex_build_info.pb",
                       "AndroidManifest.xml",
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
                        "_apex"         # Skipped all files from extracted apex folder
                        ]


# When sets allows all files to be overwritten
ALLOW_ALL_FILE_OVERWRITE = True

# "dex2oat"
SKIPPED_APEX_KEYWORD_LIST = []


ALLOWED_FILE_OVERWRITE_EXTENSION_LIST = [".ogg",
                                         ".otf",
                                         ".ttf"
                                         ]

ALLOW_FILE_OVERWRITE = ["framework-res.apk",
                        #"framework-ext-res.apk",
                        #"passwd",
                        #"group",
                        #"com.google.android.hardwareinfo.xml",
                        "services.jar.bprof",
                        "framework.jar"
                        ]

ALLOW_FILE_OVERWRITE_EXTENSIONS = [".jar"]


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
# "com.google.android.adbd.apex", --> Blocks adb access
# APEX Files in this list will be merged in the POST-INJECTOR -> APEX file not in this list will be repackaged
# in the pre-injector.
ALLOW_FILE_INJECT_ALWAYS = ["installd.rc",
                            "com.android.vndk.current.apex",
                            "com.google.android.os.statsd.apex",
                            "com.google.android.resolv.apex",
                            "com.google.android.tethering.apex",
                            "com.google.android.wifi.apex",
                            "com.google.android.cellbroadcast.apex",
                            "com.google.android.mediaprovider.apex",
                            "com.google.android.permission.apex",
                            "com.google.android.extservices.apex",
                            "com.google.android.hardwareinfo.xml",
                            "com.google.android.telephony.apex",
                            "com.google.mainline.primary.libs.apex",
                            "vndservicemanager.rc",
                            "com.android.i18n.apex",
                            "com.google.android.conscrypt.apex",
                            "com.google.android.sdkext.apex",
                            "com.google.android.ipsec.apex",
                            "com.android.runtime.apex",  # Problematic -> Without boot-framework error? Root cause of file not found error?
                            "com.google.android.art.apex",  # Problematic, contains boot.art, boot.oat, and boot.vdex
                            "com.google.android.media.swcodec.apex",
                            "com.google.android.scheduling.apex",
                            "com.google.android.appsearch.apex",
                            "com.google.android.neuralnetworks.apex",
                            "com.google.android.media.apex",
                            "com.google.android.tzdata3.apex" # Special matching tzdata3 -> tzdata
                            ]
# "com.google.android.adbd.apex"
# ALLOWED INJECTION FILES:
# "com.android.apex.cts.shim.apex"
# "com.google.android.appsearch.apex"
# "com.google.android.ipsec.apex"
# "com.google.android.os.statsd.apex"
# "com.google.android.telephony.apex"
# "com.android.i18n.apex"
# "com.google.android.art.apex"
# "com.google.android.media.apex"
# "com.google.android.permission.apex"
# "com.google.android.tethering.apex"
# "com.android.runtime.apex"
# "com.google.android.cellbroadcast.apex"
# "com.google.android.media.swcodec.apex"
# "com.google.android.resolv.apex"
# "com.google.android.tzdata3.apex"
# "com.android.vndk.current.apex"
# "com.google.android.conscrypt.apex"
# "com.google.android.mediaprovider.apex"
# "com.google.android.scheduling.apex"
# "com.google.android.wifi.apex"
# "com.google.android.extservices.apex"
# "com.google.android.neuralnetworks.apex"
# "com.google.android.sdkext.apex"
# "com.google.mainline.primary.libs.apex"



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
    "adbd": "packages/modules/adb/apex",
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
}














