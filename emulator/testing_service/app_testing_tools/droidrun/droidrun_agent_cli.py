import os
import sys
import argparse
import asyncio
import json
import datetime
import logging
from droidrun.agent.droid.droid_agent import DroidAgent, DroidrunConfig, AgentConfig
from droidrun.agent.llm.openai_like import OpenAILike
from droidrun.agent.tools.adb_tools import AdbTools

DEFAULT_CONFIG_FILE = '/android/llm_config.txt'


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
                config[key.strip()] = value.strip()
    return config


def parse_args():
    parser = argparse.ArgumentParser(description="Run Droidrun agent for a prompt on Android devices.")
    parser.add_argument('--config-file', type=str, default=DEFAULT_CONFIG_FILE,
                        help=f'Path to key=value config file for api-base and api-key (default: {DEFAULT_CONFIG_FILE})')
    parser.add_argument('--model', type=str, default=None, help='LLM model name')
    parser.add_argument('--api-base', type=str, default=None, help='OpenAI API base URL')
    parser.add_argument('--api-key', type=str, default=None, help='OpenAI API key')
    parser.add_argument('--prompt', type=str, required=True, help='Prompt for the agent')
    parser.add_argument('--max-steps', type=int, default=30, help='Maximum steps for the agent')
    parser.add_argument('--device', type=str, nargs='*', help='Device serial(s) to run on (default: all)')
    parser.add_argument('--logfile', type=str, default='droidrun_agent_log.json', help='Path to JSON logfile')

    args = parser.parse_args()

    # Load config file and apply values as defaults (CLI args take precedence)
    file_config = load_config_file(args.config_file)
    if args.api_base is None:
        args.api_base = file_config.get('api-base') or file_config.get('api_base')
    if args.api_key is None:
        args.api_key = file_config.get('api-key') or file_config.get('api_key')
    if args.model is None:
        args.model = file_config.get('model', 'llama3.2-vision')

    # Validate required values
    missing = []
    if not args.api_base:
        missing.append('api-base')
    if not args.api_key:
        missing.append('api-key')
    if missing:
        parser.error(
            f"The following required values are missing: {', '.join(missing)}. "
            f"Provide them via CLI arguments or in the config file ({args.config_file})."
        )

    return args

# Helper to serialize non-serializable types
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

async def run_agent_on_device(device_serial, args):
    tools = AdbTools()
    llm = OpenAILike(
        model=args.model,
        api_base=args.api_base,
        api_key=args.api_key,
        is_chat_model=True,
        is_function_calling_model=True
    )
    config = DroidrunConfig(
        agent=AgentConfig(
            max_steps=args.max_steps,
            reasoning=False,
            streaming=False,
            after_sleep_action=1,
        ),
    )
    agent = DroidAgent(
        llms=llm,
        goal=args.prompt,
        tools=tools,
        config=config,
        device=device_serial
    )
    log_entry = {
        'timestamp': datetime.datetime.utcnow().isoformat(),
        'device': device_serial,
        'parameters': {
            'model': args.model,
            'api_base': args.api_base,
            'prompt': args.prompt,
            'max_steps': args.max_steps,
        }
    }
    try:
        result = await agent.run()
        log_entry['result'] = result
        log_entry['success'] = result.get('success', 'N/A')
        log_entry['output'] = result.get('output', None)
        logging.info('--- Results for device %s ---', device_serial)
        logging.info('Result object: %s', result)
        logging.info('Success: %s', result.get('success', 'N/A'))
        if result.get('output'):
            logging.info('Output: %s', result['output'])
    except Exception as e:
        log_entry['error'] = str(e)
        logging.exception('Error running agent on device %s: %s', device_serial, e)
    # Write log entry to logfile (append mode)
    with open(args.logfile, 'a') as f:
        f.write(json.dumps(log_entry, cls=CustomJSONEncoder) + "\n")

async def main():
    args = parse_args()
    tools = AdbTools()
    if args.device:
        device_serials = args.device
    else:
        device_serials = tools.list_devices()  # Returns list of device serials
    if not device_serials:
        logging.error('No devices found.')
        sys.exit(1)
    # Log initial parameters
    init_log = {
        'timestamp': datetime.datetime.utcnow().isoformat(),
        'parameters': {
            'model': args.model,
            'api_base': args.api_base,
            'prompt': args.prompt,
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

