#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

# This makefile contains the system partition contents for
# a generic phone or tablet device. Only add something here if
# it definitely doesn't belong on other types of devices (if it
# does, use base_vendor.mk).
$(call inherit-product, $(SRC_TARGET_DIR)/product/media_system.mk)
$(call inherit-product-if-exists, frameworks/base/data/fonts/fonts.mk)
$(call inherit-product-if-exists, external/google-fonts/dancing-script/fonts.mk)
$(call inherit-product-if-exists, external/google-fonts/carrois-gothic-sc/fonts.mk)
$(call inherit-product-if-exists, external/google-fonts/coming-soon/fonts.mk)
$(call inherit-product-if-exists, external/google-fonts/cutive-mono/fonts.mk)
$(call inherit-product-if-exists, external/google-fonts/source-sans-pro/fonts.mk)
$(call inherit-product-if-exists, external/noto-fonts/fonts.mk)
$(call inherit-product-if-exists, external/roboto-fonts/fonts.mk)
$(call inherit-product-if-exists, external/hyphenation-patterns/patterns.mk)
$(call inherit-product-if-exists, frameworks/base/data/keyboards/keyboards.mk)
$(call inherit-product-if-exists, frameworks/webview/chromium/chromium.mk)

# Disabled Packages:
    #BasicDreams \
    #BlockedNumberProvider \
    #BluetoothMidiService \
    #BookmarkProvider \
    #BuiltInPrintService \
    #CalendarProvider \
    #CaptivePortalLogin \
    #DownloadProviderUi \
    #EasterEgg \
    #ExternalStorageProvider \
    #ManagedProvisioning \
    #MusicFX \
    #NfcNci \
    #PacProcessor \
    #PrintRecommendationService \
    #PrintSpooler \

PRODUCT_PACKAGES += \
    cameraserver \
    CameraExtensionsProxy \
    CertInstaller \
    clatd \
    FusedLocation \
    InputDevices \
    KeyChain \
    librs_jni \
    MmsService \
    MtpService \
    ProxyHandler \
    screenrecord \
    SecureElement \
    SharedStorageBackup \
    Telecom \
    TelephonyProvider \
    TeleService \
    Traceur \
    VpnDialogs \
    vr \
    Bluetooth \
    UserDictionaryProvider \
    SimAppDialog \
    DocumentsUI \
{% for line in package_name_list -%}{{ line }}{%- endfor %}

PRODUCT_SYSTEM_SERVER_APPS += \
    FusedLocation \
    InputDevices \
    KeyChain \
    Telecom \

PRODUCT_COPY_FILES += \
    frameworks/av/media/libeffects/data/audio_effects.conf:system/etc/audio_effects.conf

PRODUCT_VENDOR_PROPERTIES += \
    ro.carrier?=unknown \
    ro.config.notification_sound?=OnTheHunt.ogg \
    ro.config.alarm_alert?=Alarm_Classic.ogg

TARGET_SUPPORTS_32_BIT_APPS := false
TARGET_SUPPORTS_64_BIT_APPS := true
PRODUCT_PROPERTY_OVERRIDES += ro.control_privapp_permissions?=log
MODULE_BUILD_FROM_SOURCE := true