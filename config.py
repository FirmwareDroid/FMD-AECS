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
EMULATOR_DOCKERFILE_X8664_ABS_PATH = os.path.join(ROOT_PATH, "emulator/Dockerfile")
EMULATOR_DOCKERFILE_ARM64_ABS_PATH = os.path.join(ROOT_PATH, "emulator/Dockerfile_arm64")
DOCKER_PLATFORM_X86_64 = "linux/amd64"
DOCKER_PLATFORM_ARM64 = "linux/arm64"
FMD_FIRMWARE_BUILD_FILES_DOWNLOAD_TEMPLATE = "${url}/download/android_app/build_files"
SUPPORTED_ARCHITECTURES = ["x86_64", "arm64"]
SUPPORTED_LUNCH_TARGETS = ["sdk_phone_x86_64-userdebug",  # Android 12 / 13 "sdk_x86_64-userdebug"
                           "sdk_phone_arm64-userdebug",  # Android 12 -> Works
                           "sdk_phone_arm64-userdebug",  # Android 13 "sdk_arm64-userdebug"
                           ]
# String templates
FMD_GRAPHQL_URL_TEMPLATE = '${url}/graphql/'
FMD_AUTH_QUERY_TEMPLATE = '{"query": "query Auth ' \
                          '{tokenAuth(password: \\\"${password}\\\", username: \\\"${username}\\\") {token}}",' \
                          '"operationName": "Auth"}'
FMD_AECS_FIRMWARE_QUERY_TEMPLATE = '{"query": "query GetFirmwareIdList {aecs_firmware_id_list}",' \
                                   '"operationName": "GetFirmwareIdList"}'
FMD_CSRF_URL_TEMPLATE = "${url}/csrf/"
VERIFY_SSL = False  # You can suppress warnings with: export PYTHONWARNINGS="ignore:Unverified HTTPS request"
FILTERED_APK_FILES = ["framework-res.apk",
                      "BasicDreams.apk",
                      "SimAppDialog.apk",
                      "WallpaperBackup.apk",
                      "KeyChain.apk",
                      "PacProcessor.apk",
                      "HTMLViewer.apk",
                      "Stk.apk",
                      "PrintSpooler.apk",
                      "PartnerBookmarksProvider.apk",
                      "CameraExtensionsProxy.apk",
                      "PrintRecommendationService.apk",
                      "CaptivePortalLogin.apk",
                      "EasterEgg.apk",
                      "CarrierDefaultApp.apk",
                      "ExtShared.apk",
                      "CertInstaller.apk",
                      "BookmarkProvider.apk",
                      "CompanionDeviceManager.apk",
                      "BluetoothMidiService.apk",
                      "Bluetooth.apk",
                      "NfcNci.apk",
                      "MmsService.apk",
                      "TeleService.apk",
                      "ProxyHandler.apk",
                      "MusicFX.apk",
                      "InputDevices.apk",
                      "Tag.apk",
                      "BuiltInPrintService.apk",
                      "PackageInstaller.apk",
                      "Traceur.apk",
                      "LocalTransport.apk",
                      "ManagedProvisioning.apk",
                      "NetworkPermissionConfig.apk",
                      "SettingsProvider.apk",
                      "DocumentsUI.apk",
                      "ONS.apk",
                      "Telecom.apk",
                      "CallLogBackup.apk",
                      "DownloadProvider.apk",
                      "StatementService.apk",
                      "ContactsProvider.apk",
                      "FusedLocation.apk",
                      "NetworkStack.apk",
                      "DownloadProviderUi.apk",
                      "Shell.apk",
                      "BlockedNumberProvider.apk",
                      "MtpService.apk",
                      "DynamicSystemInstallationService.apk",
                      "CalendarProvider.apk",
                      "UserDictionaryProvider.apk",
                      "CellBroadcastLegacyApp.apk",
                      "VpnDialogs.apk",
                      "TelephonyProvider.apk",
                      "BackupRestoreConfirmation.apk",
                      "SharedStorageBackup.apk",
                      "MediaProviderLegacy.apk",
                      "SoundPicker.apk",
                      "ExternalStorageProvider.apk",
                      "SecureElement.apk",
                      "LiveWallpapersPicker.apk"]

AOSP_DEFAULT_PACKAGE_NAMES = ["Browser2",
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
                         "WallpaperPicker2"]
