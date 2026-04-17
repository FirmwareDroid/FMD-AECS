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

def parse_args():
    parser = argparse.ArgumentParser(description="Run Droidrun agent for a prompt on Android devices.")
    parser.add_argument('--model', type=str, default='llama3.2-vision', help='LLM model name')
    parser.add_argument('--api-base', type=str, required=True, help='OpenAI API base URL')
    parser.add_argument('--api-key', type=str, required=True, help='OpenAI API key')
    parser.add_argument('--prompt', type=str, required=True, help='Prompt for the agent')
    parser.add_argument('--max-steps', type=int, default=30, help='Maximum steps for the agent')
    parser.add_argument('--device', type=str, nargs='*', help='Device serial(s) to run on (default: all)')
    parser.add_argument('--logfile', type=str, default='droidrun_agent_log.json', help='Path to JSON logfile')
    return parser.parse_args()

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

