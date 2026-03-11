#!/bin/bash

# Ensure adb is installed
if ! command -v adb &> /dev/null
then
    echo "adb could not be found. Please install it first."
    exit
fi

# Disable the OEM RIL Hook Service
SERVICE_NAME="com.samsung.slsi.telephony.oem.oemrilhookservice"
echo "Disabling $SERVICE_NAME..."

# Use the `pm disable-user` command to disable the service
adb shell pm disable-user "$SERVICE_NAME"

echo "$SERVICE_NAME has been disabled."
