#!/bin/bash

# Function to grant all permissions to the app
grant_all_permissions() {
    package_name=$1

    # Get the list of all permissions
    permissions=$(adb shell pm list permissions -g -d | awk -F: '{print $2}' | tr '\n' ' ')

    # Split permissions into an array
    IFS=$' ' read -rd '' -a permissions_array <<<"$permissions"

    for permission in "${permissions_array[@]}"; do
        echo "Granting $permission to $package_name"
        adb shell pm grant $package_name $permission
    done
}

# Package name of the app
PACKAGE_NAME="com.android.systemui"

# Check if device is connected
if adb get-state 1>/dev/null 2>&1; then
    echo "Device is connected. Granting permissions..."
    grant_all_permissions $PACKAGE_NAME
    echo "Permissions granted."
else
    echo "No device connected. Please connect your Android device and try again."
fi

