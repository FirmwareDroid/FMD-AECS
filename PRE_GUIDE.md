# Preperation Guide to Setup AOSP for the Re-Hoster

This guide assume that you work on a Linux system and that you store the AOSP source code in your home foler under a folder name ´aosp´. 
For each version a subfolder needs to be created in the aosp folder. This guide gives you all the commands for each version in each step. Select the commands that correspond to your version.

```
mkdir -p ~/aosp/aosp12
mkdir -p ~/aosp/aosp12_1
mkdir -p ~/aosp/aosp13
mkdir -p ~/aosp/aosp14
```

## AOSP Installation
Download the AOSP Source code from the [official source](https://source.android.com/docs/setup/download). 

### Additional Tipps and Installation:
Download the AOSP Source Code:
- Install necessary packages on your machine
```
sudo apt install -y libncurses5 zip apksigner
```
- Download AOSP source code
	- **TIPP: in case of errors use the authenticated repo sync**: https://source.android.com/docs/setup/download/troubleshoot-sync
```
mkdir aosp_12
cd aosp_12
sudo apt install -y repo
git config --global user.email "you@example.com"
git config --global user.name "Your Name"

# With Authentication Android 11/12/13
repo init --partial-clone -b android-12.1.0_r27 -u https://android.googlesource.com/a/platform/manifest
# Android 14
repo init --partial-clone --no-use-superproject -b android-14.0.0_r67 -u https://android.googlesource.com/a/platform/manifest

# Without Authentication
repo init --partial-clone -b android-12.1.0_r27 -u https://android.googlesource.com/platform/manifest
# Android 14
repo init --partial-clone --no-use-superproject -b android-latest-release -u https://android.googlesource.com/platform/manifest
repo sync -c -j 32
```

## AOSP Modifications

### Add Certificates for signing with apksigner
- Install apksigner: `sudo apt install apksigner`
- Add keys for APK signing: ``

```
cd ~/aosp/aosp11/build/target/product/security
cd ~/aosp/aosp12/build/target/product/security
cd ~/aosp/aosp13/build/target/product/security
cd ~/aosp/aosp14/build/target/product/security
```

- 
```bash
#Android 12 or 11
openssl pkcs8 -in verity.pk8 -inform DER -nocrypt -out verity.pem && openssl pkcs12 -export -in verity.x509.pem -inkey verity.pem -out verity.p12 -name verity -passout pass: 

# Android 12, 13, 14
openssl pkcs8 -in platform.pk8 -inform DER -nocrypt -out platform.pem
openssl pkcs12 -export -in platform.x509.pem -inkey platform.pem -out platform.p12 -name platform -passout pass: && openssl pkcs8 -in media.pk8 -inform DER -nocrypt -out media.pem && openssl pkcs12 -export -in media.x509.pem -inkey media.pem -out media.p12 -name media -passout pass: && openssl pkcs8 -in networkstack.pk8 -inform DER -nocrypt -out networkstack.pem && openssl pkcs12 -export -in networkstack.x509.pem -inkey networkstack.pem -out networkstack.p12 -name networkstack -passout pass: && openssl pkcs8 -in shared.pk8 -inform DER -nocrypt -out shared.pem && openssl pkcs12 -export -in shared.x509.pem -inkey shared.pem -out shared.p12 -name shared -passout pass: && openssl pkcs8 -in testkey.pk8 -inform DER -nocrypt -out testkey.pem && openssl pkcs12 -export -in testkey.x509.pem -inkey testkey.pem -out testkey.p12 -name testkey -passout pass: && openssl pkcs8 -in shared.pk8 -inform DER -nocrypt -out shared.pem && openssl pkcs12 -export -in shared.x509.pem -inkey shared.pem -out shared.p12 -name shared -passout pass: &&
openssl pkcs8 -in bluetooth.pk8 -inform DER -nocrypt -out bluetooth.pem && openssl pkcs12 -export -in bluetooth.x509.pem -inkey bluetooth.pem -out bluetooth.p12 -name shared -passout pass:
```

### Allow Duplicates

```
nano ~/aosp/aosp11/build/target/board/emulator_arm64/BoardConfig.mk

nano ~/aosp/aosp12/build/target/board/emulator_arm64/BoardConfig.mk
nano ~/aosp/aosp12/build/target/board/emulator_arm/BoardConfig.mk

nano ~/aosp/aosp13/build/target/board/emulator_arm64/BoardConfig.mk

nano ~/aosp/aosp14/build/target/board/generic_arm64/BoardConfig.mk
```

```
BUILD_BROKEN_DUP_RULES := true
SELINUX_IGNORE_NEVERALLOWS := true
```

### Add Build Properties
**Set Permission Whitelist to log**
```
nano ~/aosp/aosp11/build/target/product/sdk_phone_arm64.mk
nano ~/aosp/aosp11/build/target/product/emulator.mk
nano ~/aosp/aosp11/device/generic/goldfish/arm64-vendor.mk


nano ~/aosp/aosp12/build/target/product/sdk_phone_arm64.mk
nano ~/aosp/aosp12/build/target/product/emulator.mk
nano ~/aosp/aosp12/device/generic/goldfish/64bitonly/product/vendor.mk
nano ~/aosp/aosp12/device/generic/goldfish/vendor.mk

nano ~/aosp/aosp13/device/generic/goldfish/64bitonly/product/vendor.mk
nano ~/aosp/aosp13/device/generic/goldfish/vendor.mk

nano ~/aosp/aosp14/device/generic/goldfish/product/generic.mk

Change this option to log
PRODUCT_PROPERTY_OVERRIDES += ro.control_privapp_permissions=log
# Add the following to the end of the file
TARGET_SUPPORTS_32_BIT_APPS := true
TARGET_SUPPORTS_64_BIT_APPS := true
PRODUCT_PROPERTY_OVERRIDES += ro.control_privapp_permissions?=log
MODULE_BUILD_FROM_SOURCE := true
PRODUCT_PROPERTY_OVERRIDES += ro.sf.lcd_density=240
```

### Disable Platform-Tests
```
nano ~/aosp/aosp11/platform_testing/build/tasks/tests/platform_test_list.mk


nano ~/aosp/aosp12/platform_testing/build/tasks/tests/platform_test_list.mk

nano ~/aosp/aosp13/platform_testing/build/tasks/tests/platform_test_list.mk

nano ~/aosp/aosp14/platform_testing/build/tasks/tests/platform_test_list.mk

```
- Comment-out these two lines
```
#ApiDemos \
#BusinessCard \
```

### Disable SELinux

```
nano ~/aosp/aosp11/system/core/init/selinux.cpp
nano ~/aosp/aosp12/system/core/init/selinux.cpp
nano ~/aosp/aosp13/system/core/init/selinux.cpp
nano ~/aosp/aosp14/system/core/init/selinux.cpp
```
```java
EnforcingStatus StatusFromProperty() {
    return SELINUX_PERMISSIVE; //in early stage, the function returns permissive status
    EnforcingStatus status = SELINUX_PERMISSIVE;
    ImportKernelCmdline([&](const std::string& key, const std::string& value) {
        if (key == "androidboot.selinux" && value == "permissive") {
            status = SELINUX_PERMISSIVE;
        }
    });

    if (status == SELINUX_ENFORCING) {
                        status = SELINUX_PERMISSIVE;
    }
    return SELINUX_PERMISSIVE;
}

bool IsEnforcing() {
    return false; //selinux returns false under any enforcing circumstances. 
    if (ALLOW_PERMISSIVE_SELINUX) {
        return StatusFromProperty() == SELINUX_PERMISSIVE;
    }
    return true;
}
```

### Disable Boring SSL Checks
- Modify init.rc to disable auto_reboot in case of booring ssl fails -> search for booring ssl service and disable it

```
nano ~/aosp/aosp11/system/core/rootdir/init.rc
nano ~/aosp/aosp12/system/core/rootdir/init.rc
nano ~/aosp/aosp13/system/core/rootdir/init.rc
```
```
service boringssl_self_test32 /system/bin/boringssl_self_test32

service boringssl_self_test32 /system/bin/boringssl_self_test32
    setenv BORINGSSL_SELF_TEST_CREATE_FLAG true # Any nonempty value counts as true
    #reboot_on_failure reboot,boringssl-self-check-failed
    stdio_to_kmsg

service boringssl_self_test64 /system/bin/boringssl_self_test64
    setenv BORINGSSL_SELF_TEST_CREATE_FLAG true # Any nonempty value counts as true
    #reboot_on_failure reboot,boringssl-self-check-failed
    stdio_to_kmsg

service boringssl_self_test_apex32 /apex/com.android.conscrypt/bin/boringssl_self_test32
    setenv BORINGSSL_SELF_TEST_CREATE_FLAG true # Any nonempty value counts as true
    #reboot_on_failure reboot,boringssl-self-check-failed
    stdio_to_kmsg

service boringssl_self_test_apex64 /apex/com.android.conscrypt/bin/boringssl_self_test64
    setenv BORINGSSL_SELF_TEST_CREATE_FLAG true # Any nonempty value counts as true
    #reboot_on_failure reboot,boringssl-self-check-failed
    stdio_to_kmsg
```

- Disable reboot_on_failure

```
nano ~/aosp/aosp11/external/boringssl/selftest/boringssl_self_test.rc
nano ~/aosp/aosp12/external/boringssl/selftest/boringssl_self_test.rc
nano ~/aosp/aosp13/external/boringssl/selftest/boringssl_self_test.rc
nano ~/aosp/aosp14/external/boringssl/selftest/boringssl_self_test.rc
```

```
service boringssl_self_test32_vendor /vendor/bin/boringssl_self_test32
    setenv BORINGSSL_SELF_TEST_CREATE_FLAG true # Any nonempty value counts as true
    #reboot_on_failure reboot,boringssl-self-check-failed
    stdio_to_kmsg

service boringssl_self_test64_vendor /vendor/bin/boringssl_self_test64
    setenv BORINGSSL_SELF_TEST_CREATE_FLAG true # Any nonempty value counts as true
    #reboot_on_failure reboot,boringssl-self-check-failed
    stdio_to_kmsg
```
### Fix produces files outside its artifact path requirement
```
nano ~/aosp/aosp11/build/make/target/product/mainline_system.mk
nano ~/aosp/aosp12/build/make/target/product/generic_system.mk
nano ~/aosp/aosp13/build/make/target/product/generic_system.mk
nano ~/aosp/aosp14/build/make/target/product/generic_system.mk
```
```
# Disable call at the end of the file
#$(call require-artifacts-in-path, $(_my_paths), $(_my_allowed_list))
```

### Disable VNDK Checks
Add the following packages to the file: 
```
nano ~/aosp/aosp11/build/soong/cc/config/vndk.go
nano ~/aosp/aosp12/build/soong/cc/config/vndk.go
nano ~/aosp/aosp13/build/soong/cc/config/vndk.go
nano ~/aosp/aosp14/build/soong/cc/config/vndk.go
```
```
        "libjpeg",
        "libwifi-system-iface",
        "libnl",
        "libvndksupport",
        "libhardware_legacy",
        "android.hardware.media.omx@1.0",
        "android.hardware.media.omx@2.0",
        "android.hardware.media.omx@3.0",
```


### Add Priv-App Permission for Launcher3
- Line 76 for "com.android.launcher3"
```
nano ~/aosp/aosp11/frameworks/base/data/etc/privapp-permissions-platform.xml

nano ~/aosp/aosp12/frameworks/base/data/etc/privapp-permissions-platform.xml

nano ~/aosp/aosp13/frameworks/base/data/etc/privapp-permissions-platform.xml

nano ~/aosp/aosp14/frameworks/base/data/etc/privapp-permissions-platform.xml

<permission name="android.permission.PACKAGE_USAGE_STATS"/>
```

### Prepare APEX packages

#### APEX Adjust Builds

- Android >= 12
```bash
sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp12/packages/modules/DnsResolver/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp12/packages/modules/Wifi/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp12/packages/modules/ExtServices/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp12/packages/modules/adb/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp12/packages/modules/Connectivity/Tethering/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp12/packages/modules/IPsec/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp12/packages/modules/NeuralNetworks/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp12/packages/apps/CellBroadcastReceiver/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp12/packages/providers/MediaProvider/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp12/packages/modules/Permission/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp12/art/build/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp12/frameworks/av/apex/Android.bp 
```

- Android 13
```bash
sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp13/packages/modules/DnsResolver/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp13/packages/modules/Wifi/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp13/packages/modules/ExtServices/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp13/packages/modules/adb/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp13/packages/modules/Connectivity/Tethering/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp13/packages/modules/IPsec/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp13/packages/modules/NeuralNetworks/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp13/packages/apps/CellBroadcastReceiver/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp13/packages/providers/MediaProvider/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp13/packages/modules/Permission/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp13/art/build/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp13/frameworks/av/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp13/packages/modules/AdServices/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp13/packages/modules/Uwb/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp13/packages/modules/SEPolicy/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp13/packages/modules/GeoTZ/apex/com.android.geotz/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp13/packages/modules/Virtualization/apex/Android.bp
```

- Android 14
```bash
sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/modules/DnsResolver/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/modules/Wifi/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/modules/ExtServices/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/modules/adb/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/modules/Connectivity/Tethering/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/modules/IPsec/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/modules/NeuralNetworks/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/apps/CellBroadcastReceiver/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/providers/MediaProvider/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/modules/Permission/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/art/build/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/frameworks/av/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/modules/AdServices/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/modules/Uwb/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/modules/GeoTZ/apex/com.android.geotz/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/modules/Virtualization/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/modules/RemoteKeyProvisioning/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/modules/ConfigInfrastructure/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/modules/CrashRecovery/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/modules/DeviceLock/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/modules/Profiling/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/modules/RuntimeI18n/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/modules/HealthFitness/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/modules/ThreadNetwork/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/apps/CellBroadcastReceiver/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/frameworks/native/data/etc/apex/Android.bp && sed -i 's/compressible: true,/compressible: false,/' ~/aosp/aosp14/packages/modules/Virtualization/compos/apex/Android.bp
```
## Disable Build Tests

Android 14
```
nano ~/aosp/aosp14/platform_testing/build/tasks/tests/instrumentation_test_list.mk
```

```
#TvSystemUITests \
```



#### Generate missing keys

- Android 12
```bash
cd ~/aosp/aosp12 && source ./build/envsetup.sh && lunch sdk_phone_arm64-userdebug &&
cd ~/aosp/aosp12/packages/modules/DnsResolver/apex &&
openssl pkcs8 -inform DER -in testcert.pk8 -out testcert.pem -nocrypt && cp testcert.pem com.android.resolv.pem && cp testcert.pk8 com.android.resolv.pk8 && cp testcert.x509.pem com.android.resolv.x509.pem && ~/aosp/aosp12/external/avb/avbtool extract_public_key --key testcert.pem --output com.android.resolv.avbpubkey
```
- Android 13
```bash
cd ~/aosp/aosp13 && source ./build/envsetup.sh && lunch sdk_phone64_arm64-userdebug &&
cd ~/aosp/aosp13/packages/modules/DnsResolver/apex &&
openssl pkcs8 -inform DER -in testcert.pk8 -out testcert.pem -nocrypt && cp testcert.pem com.android.resolv.pem && cp testcert.pk8 com.android.resolv.pk8 && cp testcert.x509.pem com.android.resolv.x509.pem && ~/aosp/aosp13/external/avb/avbtool extract_public_key --key testcert.pem --output com.android.resolv.avbpubkey
```

- Android 14
```bash
cd ~/aosp/aosp14 && source ./build/envsetup.sh && lunch sdk_phone64_arm64-ap2a-userdebug && mm avbtool && cd ~/aosp/aosp14/packages/modules/DnsResolver/apex &&
openssl pkcs8 -inform DER -in testcert.pk8 -out testcert.pem -nocrypt && cp testcert.pem com.android.resolv.pem && cp testcert.pk8 com.android.resolv.pk8 && cp testcert.x509.pem com.android.resolv.x509.pem && ~/aosp/aosp14/out/host/linux-x86/bin/avbtool extract_public_key --key testcert.pem --output com.android.resolv.avbpubkey
```

Generate key for DNSResolver APEX: 
```
nano ~/aosp/aosp11/packages/modules/DnsResolver/apex/Android.bp
nano ~/aosp/aosp12/packages/modules/DnsResolver/apex/Android.bp
nano ~/aosp/aosp13/packages/modules/DnsResolver/apex/Android.bp
nano ~/aosp/aosp14/packages/modules/DnsResolver/apex/Android.bp
```
```
# Change Test to com.android.resolv
android_app_certificate {
    name: "com.android.resolv.certificate",
     // will use cert.pk8 and cert.x509.pem
     certificate: "com.android.resolv",
}
```



**READY TO BUILD**
