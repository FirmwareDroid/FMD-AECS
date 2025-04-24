#!/bin/bash

# Ensure adb is installed
if ! command -v adb &> /dev/null
then
    echo "adb could not be found. Please install it first."
    exit
fi

# Stop the OEM RIL Hook Service
SERVICE_NAME="com.samsung.slsi.telephony.oem.oemrilhookservice"
echo "Stopping $SERVICE_NAME..."

# Use the `am force-stop` command to stop the process
adb shell am force-stop "$SERVICE_NAME"

echo "$SERVICE_NAME has been stopped."
