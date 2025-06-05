"""
Creates for all apps on the system a shortcut on the launcher via adb.
"""

import subprocess
import logging

def get_installed_apps():
    """
    Retrieves the list of installed apps on the connected Android device.
    """
    try:
        result = subprocess.run(
            ["adb", "shell", "pm", "list", "packages"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        packages = [line.replace("package:", "").strip() for line in result.stdout.splitlines()]
        return packages
    except subprocess.CalledProcessError as e:
        logging.error(f"Error retrieving installed apps: {e}")
        return []

def create_shortcut(package_name, activity_name=".MainActivity"):
    """
    Creates a launcher shortcut for the given app package using adb.
    """
    shortcut_command = (
        f"adb shell am start -a android.intent.action.CREATE_SHORTCUT "
        f"-n {package_name}/{activity_name}"
    )
    subprocess.run(shortcut_command, shell=True, check=True)
    logging.info(f"Shortcut created for: {package_name}")


def create_shortcut_via_am(package_name, activity_name=".MainActivity"):
    """
    Creates a launcher shortcut for the given app package using adb.

    :param package_name: str - The package name of the app.
    :param activity_name: str - The activity name to launch (default is .MainActivity).
    """
    try:
        shortcut_command = (
            f"adb shell am start -a android.intent.action.CREATE_SHORTCUT "
            f"-n {package_name}/{activity_name}"
        )
        subprocess.run(shortcut_command, shell=True, check=True)
        logging.info(f"Shortcut created for: {package_name}/{activity_name}")
    except subprocess.CalledProcessError as e:
        logging.error(f"Error creating shortcut for {package_name}/{activity_name}: {e}")


def get_main_activity(package_name):
    """
    Retrieves the main activity of the given app package using adb.

    :param package_name: str - The package name of the app.
    :return: str - The main activity name or None if not found.
    """
    try:
        result = subprocess.run(
            ["adb", "shell", "cmd", "package", "resolve-activity", "--brief", package_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        lines = result.stdout.splitlines()
        main_activity = None
        inside_activity_block = False

        if not lines and len(lines) > 0:
            logging.warning(f"No output received for package: {package_name}")
            return None
        logging.info(f"Lenght of lines: {len(lines)}")
        logging.info(f"lines: {lines}")
        for line in lines:
            if line.startswith(package_name):
                main_activity = line.split("/")[1].strip()

        if main_activity:
            return main_activity
        else:
            logging.warning(f"Main activity not found for package: {package_name}")
            return None
    except subprocess.CalledProcessError as e:
        logging.error(f"Error retrieving main activity for {package_name}: {e}")
        return None


def main():
    logging.basicConfig(level=logging.INFO)
    logging.info("Retrieving installed apps...")
    apps = get_installed_apps()

    if not apps:
        logging.error("No apps found or device not connected.")
        return

    logging.info(f"Found {len(apps)} apps. Creating shortcuts...")
    success_count = 0
    failure_count = 0

    for app in apps:
        main_activity = get_main_activity(app)
        if main_activity:
            try:
                create_shortcut_via_am(app, main_activity)
                success_count += 1
            except Exception as e:
                try:
                    create_shortcut(package_name, activity_name=".MainActivity")
                except Exception as e:
                    logging.error(f"Failed to create fallback shortcut for {app}: {e}")
                    failure_count += 1
        else:
            logging.warning(f"Skipping shortcut creation for {app} due to missing main activity.")
            failure_count += 1

    logging.info(f"Shortcut creation process completed. Success: {success_count}, Failed: {failure_count}")

if __name__ == "__main__":
    main()