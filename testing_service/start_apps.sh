#!/bin/bash

delay=0.1

# Get a list of all installed packages
packages=$(adb shell pm list packages | cut -f 2 -d ":")

# Loop through each package to find and launch the main activity
for package in $packages; do
    # Get the main activity of the package by querying the package manager
    main_activity=$(adb shell pm dump "$package" | grep -A 1 "MAIN" | grep "$package" | awk '{print $2}')

    if [[ "$package" == "com.android.settings" || "$package" == "com.android.systemui" ]]; then
        echo "Skipping $package."
        continue
    fi

    # Check if a main activity is found
    if [[ -n "$main_activity" ]]; then
        echo "Launching $package ($main_activity)..."
        adb shell am start -n "$main_activity"
    else
        echo "No launchable main activity found for $package."
    fi
    sleep "$delay"
done
