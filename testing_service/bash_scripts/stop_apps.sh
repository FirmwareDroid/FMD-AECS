#!/bin/bash

# Ensure adb is installed
if ! command -v adb &> /dev/null
then
    echo "adb could not be found. Please install it first."
    exit
fi

# Get a list of all running app package names
running_apps=$(adb shell pm list packages | cut -f 2 -d ':')

# Iterate over each app and force stop it
for app in $running_apps
do
    echo "Force stopping $app"
    adb shell am force-stop "$app"
done

echo "All apps have been force stopped."
