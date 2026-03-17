#!/bin/bash

# Ensure adb is installed
if ! command -v adb &> /dev/null
then
    echo "adb could not be found. Please install it first."
    exit
fi

# Stop the RadioConfigService
echo "Stopping RadioConfigService..."
adb shell am stopservice --user 0 com.android.phone/.RadioConfigService

echo "RadioConfigService has been stopped."
