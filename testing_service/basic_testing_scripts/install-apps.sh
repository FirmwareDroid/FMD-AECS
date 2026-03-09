#!/bin/bash

# Ensure ADB is in your PATH, or specify the full path to the adb executable
ADB=adb

# Get the list of all APK files on the device
APK_FILES=$($ADB shell find / -type f -name "*.apk" 2>/dev/null)

# Function to install an APK file
install_apk() {
    local apk=$1
    echo "Installing $apk"
    $ADB shell pm install "$apk"
}

# Install each APK file found in parallel
for APK in $APK_FILES; do
    if [[ -n "$APK" ]]; then
        install_apk "$APK" &
    fi
done

# Wait for all background processes to complete
wait

echo "Installation of all APK files completed."

