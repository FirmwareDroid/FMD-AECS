import os
import sys
import argparse
import asyncio
import json
import datetime
import logging
import subprocess
import time

# Latest mobilerun imports
from mobilerun import MobileAgent, MobileConfig

DEFAULT_CONFIG_FILE = '/android/llm_config.txt'

EXPLORATION_PROMPT = (
    "Explore this application thoroughly to maximize UI code coverage. "
    "Navigate through all available tabs, menus, settings, and buttons. "
    "Scroll through lists and interact with different elements to uncover hidden views. "
    "CRITICAL INSTRUCTION: If you encounter any crash dialogs, error messages, permission pop-ups, "
    "or system warnings, immediately dismiss them (tap 'OK', 'Cancel', 'Close', or 'Deny') "
    "and resume exploring the main app. Avoid repeating the exact same sequence of actions. "
    "If you are stuck in a loop or a dead end, use the back button to return to the previous screen "
    "and choose a different path."
)


def load_config_file(config_path):
    """Load key=value pairs from a config file. Returns a dict."""
    config = {}
    if not os.path.isfile(config_path):
        return config
    with open(config_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, _, value = line.partition('=')
                config[key.strip()] = value.strip().strip('"').strip("'")
    return config


def list_adb_devices():
    """Returns a list of connected ADB device serials."""
    try:
        output = subprocess.check_output(['adb', 'devices']).decode('utf-8')
        lines = output.strip().split('\n')[1:]
        return [line.split()[0] for line in lines if 'device' in line and not line.startswith('*')]
    except Exception as e:
        logging.error("Failed to list ADB devices: %s", e)
        return []


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run Mobilerun agent for UI testing and exploration on Android devices.")
    parser.add_argument('--config-file', type=str, default=DEFAULT_CONFIG_FILE,
                        help=f'Path to key=value config file (default: {DEFAULT_CONFIG_FILE})')
    parser.add_argument('--model', type=str, default=None, help='LLM model name')
    parser.add_argument('--api-base', type=str, default=None, help='OpenAI API base URL')
    parser.add_argument('--api-key', type=str, default=None, help='OpenAI API key')
    parser.add_argument('--gemini-api-key', type=str, default=None, help='Gemini API key')
    parser.add_argument('--prompt', type=str, default=None, help='Prompt for the agent')
    parser.add_argument('--package', type=str, default=None,
                        help='Package name to launch and aggressively explore (e.g., com.example.app)')
    parser.add_argument('--max-steps', type=int, default=30,
                        help='Maximum steps for the agent (Defaults to 150 if --package is used)')
    parser.add_argument('--device', type=str, nargs='*', help='Device serial(s) to run on (default: all)')
    parser.add_argument('--logfile', type=str, default='mobilerun_agent_log.json', help='Path to JSON logfile')
    parser.add_argument('--setup-device', action='store_true',
                        help='Automatically install Mobilerun, grant permissions, and set keyboard via ADB')
    parser.add_argument('--test-wifi', action='store_true', help='Run a quick test to open Settings and enable Wi-Fi')

    args = parser.parse_args()

    if not args.setup_device and not args.test_wifi and not args.prompt and not args.package:
        parser.error(
            "the following arguments are required: --prompt (unless --setup-device, --test-wifi, or --package is specified)")

    file_config = load_config_file(args.config_file)

    if args.api_base is None:
        args.api_base = file_config.get('api-base') or file_config.get('api_base') or file_config.get('OPENAI_BASE_URL')
    if args.api_key is None:
        args.api_key = file_config.get('api-key') or file_config.get('api_key') or file_config.get('OPENAI_API_KEY')
    if args.gemini_api_key is None:
        args.gemini_api_key = (
                file_config.get('gemini-api-key') or file_config.get('gemini_api_key') or
                file_config.get('GOOGLE_API_KEY') or file_config.get('GEMINI_API_KEY')
        )
    if args.model is None:
        args.model = file_config.get('model') or file_config.get('MODEL')

    if args.model is None:
        if args.gemini_api_key:
            args.model = 'gemini-1.5-flash'  # Fast model for high-speed UI testing
        else:
            args.model = 'llama3.2-vision'

    # Auto-adjust steps for 5-minute exploration if package is provided
    if args.package and args.max_steps == 30:
        args.max_steps = 150

    return args


class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (int, float, str, bool, type(None))):
            return obj
        if hasattr(obj, '__dict__'):
            return obj.__dict__
        if isinstance(obj, bytes):
            return obj.decode(errors='replace')
        try:
            return str(obj)
        except Exception:
            return 'unserializable'


def check_and_install_mobilerun(device_serial):
    try:
        output = subprocess.check_output(
            ["adb", "-s", device_serial, "shell", "pm", "list", "packages", "com.mobilerun.portal"]).decode(
            'utf-8').strip()
        if "com.mobilerun.portal" in output:
            logging.info("Mobilerun Portal is already installed on device: %s", device_serial)
            return True
    except Exception as e:
        logging.warning("Could not check package status on device %s: %s", device_serial, e)

    logging.info("Mobilerun Portal is NOT installed on %s. Installing via 'mobilerun setup'...", device_serial)
    try:
        subprocess.run(["mobilerun", "setup"], check=True)
        return True
    except subprocess.CalledProcessError:
        logging.error("Failed to install Mobilerun Portal.")
        return False


def setup_mobilerun_keyboard(device_serial):
    logging.info('Configuring Mobilerun Keyboard for device: %s', device_serial)
    try:
        output = subprocess.check_output(["adb", "-s", device_serial, "shell", "ime", "list", "-a"]).decode('utf-8')
        ime_id = None
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("com.mobilerun.portal") and ":" in line:
                ime_id = line.split(":")[0].strip()
                break

        if ime_id:
            subprocess.run(["adb", "-s", device_serial, "shell", "ime", "enable", ime_id], check=False,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["adb", "-s", device_serial, "shell", "ime", "set", ime_id], check=False,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception as e:
        logging.warning("Failed to configure keyboard on device %s: %s", device_serial, e)


def setup_mobilerun_permissions(device_serial):
    logging.info('--- Starting setup for device: %s ---', device_serial)
    if not check_and_install_mobilerun(device_serial):
        return

    pkg = "com.mobilerun.portal"
    a11y_service = f"{pkg}/{pkg}.PortalAccessibilityService"
    listener_service = f"{pkg}/{pkg}.PortalNotificationListenerService"

    subprocess.run(["adb", "-s", device_serial, "shell", "appops", "set", pkg, "ACCESS_RESTRICTED_SETTINGS", "allow"],
                   check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["adb", "-s", device_serial, "shell", "appops", "set", pkg, "SYSTEM_ALERT_WINDOW", "allow"],
                   check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["adb", "-s", device_serial, "shell", "appops", "set", pkg, "MANAGE_EXTERNAL_STORAGE", "allow"],
                   check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    perms = ["android.permission.READ_EXTERNAL_STORAGE", "android.permission.WRITE_EXTERNAL_STORAGE",
             "android.permission.POST_NOTIFICATIONS"]
    for p in perms:
        subprocess.run(["adb", "-s", device_serial, "shell", "pm", "grant", pkg, p], check=False,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    try:
        current_a11y = subprocess.check_output(["adb", "-s", device_serial, "shell", "settings", "get", "secure",
                                                "enabled_accessibility_services"]).decode('utf-8').strip()
        new_a11y = a11y_service if (current_a11y == "null" or not current_a11y) else (
            current_a11y if a11y_service in current_a11y else current_a11y + ":" + a11y_service)
        subprocess.run(
            ["adb", "-s", device_serial, "shell", "settings", "put", "secure", "enabled_accessibility_services",
             new_a11y], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["adb", "-s", device_serial, "shell", "settings", "put", "secure", "accessibility_enabled", "1"],
                       check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception as e:
        logging.warning("Could not set accessibility services safely: %s", e)

    subprocess.run(["adb", "-s", device_serial, "shell", "cmd", "notification", "allow_listener", listener_service],
                   check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    setup_mobilerun_keyboard(device_serial)
    logging.info('Finished setting up configuration for device: %s', device_serial)


def start_target_app(device_serial, package_name):
    """Launches the target application via ADB before the agent starts exploring."""
    logging.info('Launching target package: %s on device: %s', package_name, device_serial)
    try:
        # 'monkey' is the most robust way to start the default launcher activity of any package
        subprocess.run(
            ["adb", "-s", device_serial, "shell", "monkey", "-p", package_name, "1"],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        # Give the app a moment to load its initial screen
        time.sleep(3)
    except Exception as e:
        logging.error("Failed to launch app %s: %s", package_name, e)


def apply_speed_optimizations(config: MobileConfig, max_steps: int) -> MobileConfig:
    """Configures the agent for maximum execution speed."""
    if hasattr(config, "agent"):
        config.agent.max_steps = max_steps
        # Drastically reduce wait time after actions (200ms instead of 1s+)
        config.agent.after_sleep_action = 0.2
        # Disable LLM text reasoning to save token generation time
        config.agent.reasoning = False
        # Disable streaming overhead
        config.agent.streaming = False
    return config


async def run_agent_on_device(device_serial, args):
    if args.api_key:
        os.environ["OPENAI_API_KEY"] = args.api_key
    if args.api_base:
        os.environ["OPENAI_BASE_URL"] = args.api_base
    if args.gemini_api_key:
        os.environ["GEMINI_API_KEY"] = args.gemini_api_key
        os.environ["GOOGLE_API_KEY"] = args.gemini_api_key

    config = MobileConfig()

    # 1. Apply speed tuning
    config = apply_speed_optimizations(config, args.max_steps)

    if hasattr(config, "device"):
        config.device.serial = device_serial

    if hasattr(config, "llm_profiles"):
        for profile_name, profile in config.llm_profiles.items():
            profile.model = args.model
            if args.api_base:
                profile.base_url = args.api_base

    # 2. Launch the App if specified
    if args.package:
        start_target_app(device_serial, args.package)

    agent = MobileAgent(
        goal=args.prompt,
        config=config
    )

    log_entry = {
        'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'device': device_serial,
        'parameters': {
            'model': args.model,
            'api_base': args.api_base,
            'prompt': args.prompt,
            'package': args.package,
            'max_steps': args.max_steps,
        }
    }

    try:
        logging.info("Starting MobileAgent execution on %s (Max Steps: %s)...", device_serial, args.max_steps)
        result = await agent.run()
        log_entry['result'] = result
        log_entry['success'] = getattr(result, 'success', 'N/A')
        log_entry['reason'] = getattr(result, 'reason', None)
        log_entry['steps'] = getattr(result, 'steps', None)

        logging.info('--- Results for device %s ---', device_serial)
        logging.info('Success: %s', log_entry['success'])
        if log_entry['reason']:
            logging.info('Reason: %s', log_entry['reason'])
    except Exception as e:
        log_entry['error'] = str(e)
        logging.exception('Error running agent on device %s: %s', device_serial, e)

    with open(args.logfile, 'a') as f:
        f.write(json.dumps(log_entry, cls=CustomJSONEncoder) + "\n")


async def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    args = parse_args()

    if args.device:
        device_serials = args.device
    else:
        device_serials = list_adb_devices()

    if not device_serials:
        logging.error('No connected ADB devices found.')
        sys.exit(1)

    if args.setup_device:
        for serial in device_serials:
            setup_mobilerun_permissions(serial)

    if args.test_wifi:
        args.prompt = "Tap 'Network & internet', then tap the toggle next to 'Wi-Fi'."
        logging.info('Test mode activated. Running fast Wi-Fi enablement test.')

    if args.package and not args.prompt:
        args.prompt = EXPLORATION_PROMPT
        logging.info('App exploration mode activated for package: %s', args.package)

    if not args.prompt:
        logging.info('Setup execution complete. No prompt or app provided, skipping agent execution.')
        return

    init_log = {
        'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'parameters': {
            'model': args.model,
            'prompt': args.prompt,
            'package': args.package,
            'max_steps': args.max_steps,
            'devices': device_serials,
        }
    }

    with open(args.logfile, 'a') as f:
        f.write(json.dumps(init_log, cls=CustomJSONEncoder) + "\n")

    tasks = [run_agent_on_device(serial, args) for serial in device_serials]
    await asyncio.gather(*tasks)


if __name__ == '__main__':
    asyncio.run(main())