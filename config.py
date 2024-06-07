import os

ROOT_PATH = os.path.dirname(os.path.realpath(__file__))
AOSP_PACKAGES_APPS_PATH = "packages/apps/"
META_BUILD_SYSTEM_FILENAME = "meta_build_system.txt"
META_BUILD_VENDOR_FILENAME = "meta_build_vendor.txt"
META_BUILD_PRODUCT_FILENAME = "meta_build_product.txt"
META_BUILD_FILENAMES = [META_BUILD_SYSTEM_FILENAME, META_BUILD_VENDOR_FILENAME, META_BUILD_PRODUCT_FILENAME]
TEMPLATE_FOLDER = "templates/"
BASE_PATH = "build/make/target/product/"
BASE_PRODUCT_FILE_NAME = "base_product.mk"
BASE_SYSTEM_FILE_NAME = "base_system.mk"
BASE_VENDOR_FILE_NAME = "base_vendor.mk"
BASE_FILENAMES = [BASE_PRODUCT_FILE_NAME, BASE_SYSTEM_FILE_NAME, BASE_VENDOR_FILE_NAME]
BUILD_OUT_PATH = os.path.join(ROOT_PATH, "out/")
IMAGE_ARTEFACTS_ARM64_PATH = "image_artefacts/arm64-v8a/"
IMAGE_ARTEFACTS_X86_64_PATH = "image_artefacts/x86_64/"
IMAGE_ARTEFACTS_PATH = "image_artefacts/"
AOSP_BUILD_OUT_SDK_ARM64_PATH = "out/target/product/emulator_arm64/"
AOSP_BUILD_OUT_SDK_x86_64_PATH = "out/target/product/emulator_x86_64/"
AOSP_EMU_ZIP_FILENAME = f"sdk-repo-linux-system-images-eng.{os.getlogin()}.zip"
IMAGE_ARTEFACTS_X86_64_ABS_PATH = os.path.join(ROOT_PATH, IMAGE_ARTEFACTS_X86_64_PATH)
IMAGE_ARTEFACTS_ABS_PATH = os.path.join(ROOT_PATH, IMAGE_ARTEFACTS_PATH)
EMULATOR_IMG_ABS_PATH = os.path.join(ROOT_PATH, "emulator_images/")
EMULATOR_DOCKERFILE_X8664_ABS_PATH = os.path.join(ROOT_PATH, "emulator/Dockerfile_x86_64")
EMULATOR_DOCKERFILE_ARM64_ABS_PATH = os.path.join(ROOT_PATH, "emulator/Dockerfile_arm64")
EMULATOR_DOCKERFILE_BASE_ABS_PATH = os.path.join(ROOT_PATH, "emulator/Dockerfile_base_emulator_")
NEXUS_SERVICE_ENDPOINT = "service/extdirect"
NEXUS_EMULATOR_REPOSITORY = "repository/emulator-images/"
NEXUS_DOCKER_EMULATOR_REPOSITORY = "repository/docker-emulator-images/"
DOCKER_PLATFORM_X86_64 = "linux/amd64"
DOCKER_PLATFORM_ARM64 = "linux/arm64"
FMD_FIRMWARE_BUILD_FILES_DOWNLOAD_TEMPLATE = "${url}/download/android_app/build_files"
SUPPORTED_ARCHITECTURES = ["x86_64", "arm64"]
SUPPORTED_LUNCH_TARGETS = ["sdk_phone_x86_64-userdebug",  # Android 12 / 13 "sdk_x86_64-userdebug"
                           "sdk_phone_arm64-userdebug",  # Android 12 -> Works
                           "sdk_phone_arm64-userdebug",  # Android 13 "sdk_arm64-userdebug"
                           ]
BUILD_RETRY_COUNT = 1
# String templates
FMD_GRAPHQL_URL_TEMPLATE = '${url}/graphql/'
FMD_AUTH_QUERY_TEMPLATE = '{"query": "query Auth ' \
                          '{tokenAuth(password: \\\"${password}\\\", username: \\\"${username}\\\") {token}}",' \
                          '"operationName": "Auth"}'
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
VERIFY_SSL = False  # You can suppress warnings with: export PYTHONWARNINGS="ignore:Unverified HTTPS request"
AOSP_DEFAULT_PACKAGE_NAMES = ["framework-res",
                              "BasicDreams",
                              "SimAppDialog",
                              "WallpaperBackup",
                              "KeyChain",
                              "PacProcessor",
                              "HTMLViewer",
                              "Stk",
                              "PrintSpooler",
                              "PartnerBookmarksProvider",
                              "CameraExtensionsProxy",
                              "PrintRecommendationService",
                              "CaptivePortalLogin",
                              "EasterEgg",
                              "CarrierDefaultApp",
                              "ExtShared",
                              "CertInstaller",
                              "BookmarkProvider",
                              "CompanionDeviceManager",
                              "BluetoothMidiService",
                              "Bluetooth",
                              "NfcNci",
                              "MmsService",
                              "TeleService",
                              "ProxyHandler",
                              "MusicFX",
                              "InputDevices",
                              "Tag",
                              "BuiltInPrintService",
                              "PackageInstaller",
                              "Traceur",
                              "LocalTransport",
                              "ManagedProvisioning",
                              "NetworkPermissionConfig",
                              "SettingsProvider",
                              "DocumentsUI",
                              "ONS",
                              "Telecom",
                              "CallLogBackup",
                              "DownloadProvider",
                              "StatementService",
                              "ContactsProvider",
                              "FusedLocation",
                              "NetworkStack",
                              "DownloadProviderUi",
                              "Shell",
                              "BlockedNumberProvider",
                              "MtpService",
                              "DynamicSystemInstallationService",
                              "CalendarProvider",
                              "UserDictionaryProvider",
                              "CellBroadcastLegacyApp",
                              "VpnDialogs",
                              "TelephonyProvider",
                              "SharedStorageBackup",
                              "MediaProviderLegacy",
                              "SoundPicker",
                              "ExternalStorageProvider",
                              "SecureElement",
                              "LiveWallpapersPicker",
                              "Browser2",
                              "CarrierConfig",
                              "DeskClock",
                              "EmergencyInfo",
                              "ImsServiceEntitlement",
                              "ManagedProvisioning",
                              "Nfc",
                              "Protips",
                              "RemoteProvisioner",
                              "Settings",
                              "StorageManager",
                              "ThemePicker",
                              "TvSettings",
                              "Calendar",
                              "CellBroadcastReceiver",
                              "DevCamera",
                              "Gallery",
                              "KeyChain",
                              "Messaging",
                              "OnDeviceAppPrediction",
                              "Provision",
                              "SafetyRegulatoryInfo",
                              "SettingsIntelligence",
                              "TV",
                              "TimeZoneData",
                              "UniversalMediaPlayer",
                              "BasicSmsReceiver",
                              "Camera2",
                              "CertInstaller",
                              "Dialer",
                              "Gallery2",
                              "Launcher3",
                              "Music",
                              "OneTimeInitializer",
                              "QuickAccessWallet",
                              "SampleLocationAttribution",
                              "SpareParts",
                              "Tag",
                              "TimeZoneUpdater",
                              "WallpaperPicker",
                              "Bluetooth",
                              "Car",
                              "Contacts",
                              "DocumentsUI",
                              "HTMLViewer",
                              "LegacyCamera",
                              "MusicFX",
                              "PhoneCommon",
                              "QuickSearchBox",
                              "SecureElement",
                              "Stk",
                              "Test",
                              "Traceur",
                              "WallpaperPicker2",
                              "BackupRestoreConfirmation",
                              "BasicDreams",
                              "CellBroadcastServiceModulePlatform",
                              "CtsShim",
                              "CtsShimPriv",
                              "InProcessNetworkStack",
                              "MediaProvider",
                              "ImageWallpaper",
                              "LivePicker",
                              "AlternativeNetworkAccess",
                              "Iwlan",
                              "PlatformCaptivePortalLogin",
                              "PermissionController",
                              "PlatformNetworkPermissionConfig",
                              "framework-ext-res",
                              "com.android.provision.xml",
                              "Provision",
                              "PhotoTable",
                              "SystemUI",
                              "WallpaperCropper",
                              "SettingsLib",
                              "WindowManager",
                              "AppPredictionLib",
                              "Keyguard",
                              "WAPPushManager",
                              "Backup",
                              "FakeOemFeatures",
                              "BackupEncryption",
                              "EncryptedLocalTransport",
                              "MtpDocumentsProvider",
                              "Tethering",
                              "framework-res__auto_generated_rro_vendor",
                              "ModuleMetadata",
                              "ExtServices",
                              "CarrierDefaultApp"]

# List of packages that are not allowed to be included in the firmware because they
# cause the firmware to fail to start
VENDOR_BLACKLISTED_PACKAGES = ["GooglePermissionController",  # Singleton App - Breaks PermissionController
                               "ServiceWifiResources",  # Breaks NetworkStack
                               "CarrierWifi",  # Breaks NetworkStack
                               "WifiDialog",  # Breaks NetworkStack
                               "Cidmanager",  # Breaks telephony / systemui - software updater
                               "GooglePackageInstaller",  # Singleton App - Breaks PackageInstaller
                               "ServiceConnectivityResourcesGoogle",  # Breaks NetworkStack - Resources not found
                               "GmsCore",  # Breaks NetworkStack / Telephony
                               "NetworkPermissionConfigGoogle"
                               #"SamsungMultiConnectivity",              # Breaks NetworkStack
                               ]

BLACKLISTED_KEYWORDS = ["Overlay", "Connectivity", "Wifi", "Telephony", "Telecom", "TeleService", "TelephonyProvider",
                        "NetworkStackGoogle"]
