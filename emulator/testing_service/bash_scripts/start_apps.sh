#!/bin/bash

delay=0.3

# Get a list of all installed packages
packages=$(adb shell pm list packages | cut -f 2 -d ":")

# Loop through each package to find and launch the main activity
for package in $packages; do
    RESULT=$(adb shell monkey -p "$package" -c android.intent.category.LAUNCHER 1 2>&1)
    sleep "$delay"
    PID=$(adb shell pidof "$package")
    if [ -n "$PID" ]; then
        echo "✅ Success: $PACKAGE is running (PID: $PID)"
    else
        # Get the main activity of the package by querying the package manager
        #main_activity=$(adb shell pm dump "$package" | grep -A 1 "MAIN" | grep "$package" | awk '{print $2}')
        main_activity=$(adb shell "cmd package resolve-activity --components $package" | grep "component=" | cut -d'=' -f2 | cut -d' ' -f1)
        if [[ -n "$main_activity" ]]; then
          echo "Launching $package ($main_activity)..."
          adb shell am start -n "$main_activity"
        else
          echo "❌ Failed: No launchable main activity found for $package."
        fi
    fi
    sleep "$delay"
done
