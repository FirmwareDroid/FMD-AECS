import subprocess
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict

def get_apk_files():
    """
    Fetches the list of all APK files on the connected Android device.
    :return: List of APK file paths.
    """
    try:
        result = subprocess.run(
            ["adb", "shell", "find", "/", "-type", "f", "-name", "*.apk"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return result.stdout.strip().split("\n")
    except Exception as e:
        print(f"Error fetching APK files: {e}")
        return []

def filter_apk_install_list(apk_list):
    """
    Filters out APKs whose paths contain '/apex/' or '/overlay/'.

    :param apk_list: List of APK file paths.
    :return: Filtered list of APK file paths.
    """
    return [apk for apk in apk_list if "/apex/" not in apk and "/overlay/" not in apk]

def install_apk(apk_path, results):
    """
    Installs a single APK file on the connected Android device and tracks results.
    :param apk_path: Path to the APK file on the device.
    :param results: Dictionary to track success and failure counts.
    """
    try:
        print(f"Installing {apk_path}")
        result = subprocess.run(
            ["adb", "shell", "pm", "install", apk_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode == 0:
            print(f"Successfully installed {apk_path}")
            results["success"] += 1
        else:
            error_message = result.stderr.strip() or result.stdout.strip() or "Unknown error"
            print(f"Failed to install {apk_path}: {error_message}")
            results["failures"]["count"] += 1
            results["failures"]["details"][error_message] += 1
    except Exception as e:
        error_message = str(e)
        print(f"Error installing {apk_path}: {error_message}")
        results["failures"]["count"] += 1
        results["failures"]["details"][error_message] += 1

def main():
    apk_files = get_apk_files()
    if not apk_files:
        print("No APK files found on the device.")
        return

    apk_filtered_list = filter_apk_install_list(apk_files)

    # Initialize results dictionary
    results = {
        "success": 0,
        "failures": {
            "count": 0,
            "details": defaultdict(int)
        }
    }

    with ThreadPoolExecutor() as executor:
        executor.map(lambda apk: install_apk(apk, results), apk_filtered_list)

    # Print summary
    print("\n📊 Installation Summary:")
    print(f"  ✅ Successfully installed: {results['success']}")
    print(f"  ❌ Failed installations: {results['failures']['count']}")

    if results["failures"]["count"] > 0:
        print("\n⚠️ Failure Details:")
        for error, count in results["failures"]["details"].items():
            print(f"  {error}: {count} occurrences")

if __name__ == "__main__":
    main()