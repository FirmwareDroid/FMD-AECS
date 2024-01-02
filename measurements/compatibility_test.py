"""
Experimental scripts to measure if the installation of specific app packages worked.
"""
import ast
import collections
import os
import subprocess
import time

SERVER = "160.85.30.12:5555"
INPUT_FILE_PATH = "./apk_meta.txt"
USER_ID = "1001"


def extract_exceptions(log_file_path):
    exception_list = []
    exception_name_list = []
    with open(log_file_path, 'r', encoding='utf-8', errors='ignore') as log_file:
        for line in log_file:
            #print(line)
            if "Exception" in line or "Error" in line:
                #print(f"found match: {line}")
                exception_list.append(line)
                word_list = line.split()
                for word in word_list:
                    if "Exception" in word or "Error" in word:
                        exception_name_list.append(word)
    return exception_list, exception_name_list


def start_test():
    # print(f"Attempt to connect {SERVER}")
    adb_connect_process = subprocess.Popen(f'adb connect {SERVER}', shell=True, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT)
    # print("Cot connection. Attempt to get package list")

    adb_pm_process = subprocess.Popen('adb shell cmd package list packages', shell=True, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT)
    installed_package_list = []
    for line in adb_pm_process.stdout.readlines():
        installed_package_list.append(line.decode("utf-8").replace("package:", "").replace("\n", ""))
    retval = adb_pm_process.wait()
    # print(f"Got package list: {len(installed_package_list)}\n{installed_package_list}")

    if adb_pm_process.returncode != 0:
        print("Couldn't get adb package list")
        exit(1)

    is_install_success_counter = 0
    is_start_success_counter = 0
    is_start_fail_counter = 0
    is_app_with_ui_counter = 0
    is_app_without_ui_counter = 0
    error_counter = {"security": 0, "not_found": 0, "others": 0}

    logcat_exception_list = []
    failed_activity_list = []
    failed_apk_list = []
    # print("Parse apk meta file")
    f = open(INPUT_FILE_PATH, "r")
    line_list = f.readlines()
    total_app_counter = len(line_list)

    subprocess.Popen('adb logcat -c', shell=True)
    main_log_process = subprocess.Popen('adb logcat "*:E" > ./logcat_errors.txt', shell=True)

    for line in line_list:
        data = line.split(":")
        apk_name = data[0]
        package_name = data[1]
        if package_name in installed_package_list:
            is_install_success_counter += 1

        activity_list = ast.literal_eval(data[2])

        print(f"Test apk: {package_name}:{apk_name}:{len(activity_list)}")

        log_file_name = f"./logs/{package_name}.txt"
        open(log_file_name, 'a').close()
        log_process = subprocess.Popen(f'adb logcat "*:E" -e {package_name} '
                                       f' > {log_file_name}', shell=True)
        log_size = 0
        if len(activity_list) > 0:
            is_app_with_ui_counter += 1
            is_launch_success = False
            warning_counter = 0
            for activity in activity_list:
                activity = activity.replace("..", ".")
                print(f"\t: {activity}")
                p1 = subprocess.Popen(f'adb shell am force-stop {package_name}', shell=True)
                p1.wait()

                subprocess.Popen(f'adb shell am start -n {package_name}/{activity}',
                                 shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                activity_process = subprocess.Popen(f'adb shell am start -n {package_name}/{activity}',
                                                    shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                activity_process.wait()

                log_size_new = os.path.getsize(log_file_name)
                has_logcat_errors = False
                if log_size != log_size_new:
                    log_size = log_size_new
                    has_logcat_errors = True

                _, error = activity_process.communicate()
                error = error.decode("utf-8")

                if error or has_logcat_errors:
                    if "does not exist" in error:
                        error_counter["not_found"] += 1
                    elif "security" in error or "Permission Denial" in error:
                        error_counter["security"] += 1
                    else:
                        if "Warning:" in error:
                            if warning_counter < 2:
                                activity_list.append(activity)
                                warning_counter += 1
                                subprocess.Popen('adb shell am start -a android.intent.action.MAIN '
                                                 '-c android.intent.category.HOME', shell=True)
                                #time.sleep(2)
                        else:
                            error_counter["others"] += 1
                            print("\tLikely app crash error - check logcat")
                            #print(error)
                    p1 = subprocess.Popen(f'adb shell am force-stop {package_name}', shell=True)
                    p1.wait()
                else:
                    time.sleep(3)
                    is_launch_success = True
                    print("\tTest success")
                    break
                time.sleep(2)
                log_clear_p = subprocess.Popen('adb logcat -c', shell=True)
                log_clear_p.wait()

            if is_launch_success:
                is_start_success_counter += 1
            else:
                print("\tTest failed")
                is_start_fail_counter += 1
                exception_list, exception_name_list = extract_exceptions(log_file_name)
                logcat_exception_list.extend(exception_name_list)
                failed_apk_list.append(f"{apk_name}:{package_name}:{activity_list}\n{exception_list}\n")
        else:
            is_app_without_ui_counter += 1
            # service_process = subprocess.Popen(f'adb shell am start-service -n {package_name}/{activity}', shell=True)
            # service_process.wait()

        log_process.kill()

        error_log_size = os.path.getsize(log_file_name)
        if error_log_size == 0:
            os.remove(log_file_name)

    main_log_process.kill()

    print("---------------------")
    print("Failed packages:")
    for failed_app in failed_apk_list:
        print(failed_app)
    print("---------------------------------------------------------------")
    print("Tests completed")
    print(f"Install success: {is_install_success_counter} out of {total_app_counter}")
    print(f"Apps without UI: {is_app_without_ui_counter}")
    print(f"Apps with UI: {is_app_with_ui_counter}")
    print(f"\tStarting App UI success: {is_start_success_counter}")
    print(f"\tStarting App UI failed: {is_start_fail_counter}")
    print(f"\t\tActivity Security/Access Errors: {error_counter['security']}")
    print(f"\t\tActivity Not Found Errors: {error_counter['not_found']}")
    print(f"\t\tActivity Other Errors: {error_counter['others']}")
    print(f"\t\tLogcat Error Counter: {collections.Counter(logcat_exception_list)}")
    print("---------------------------------------------------------------")


    # adb shell monkey -p com.estrongs.android.pop -v 500


if __name__ == "__main__":
    start_test()
    #print(extract_exceptions("./logs/com.android.gallery3d.txt"))
