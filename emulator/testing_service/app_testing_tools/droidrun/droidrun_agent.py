import asyncio
import os
import logging
from dotenv import load_dotenv
from llama_index.llms.openai_like import OpenAILike
from droidrun import DroidAgent, AdbTools, DroidrunConfig, AgentConfig, DeviceConfig, LoggingConfig, TracingConfig

# Load environment variables from .env file
load_dotenv()


async def main():
    # Load adb tools
    tools = AdbTools()

    # Initialize the LLM using environment variables
    llm = OpenAILike(
        model=os.getenv("DROID_MODEL", "llama3.2-vision"),
        api_base=os.getenv("OPENAI_API_BASE"),
        api_key=os.getenv("OPENAI_API_KEY"),
        is_chat_model=True,
        is_function_calling_model=True
    )

    # See https://docs.droidrun.ai/v5/sdk/configuration
    config = DroidrunConfig(
        agent=AgentConfig(
            max_steps=30,
            reasoning=False,
            streaming=False,
            after_sleep_action=1,
        ),
    )
    # Create the DroidAgent
    agent = DroidAgent(
        llms=llm,  # Move LLM to the first position
        goal="Open Settings and explore each section or sub-menu to find any interesting features or options.",
        tools=tools,
        config=config
    )

    # Run the agent
    result = await agent.run()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    logging.info('--- Results ---')
    logging.info('Result object: %s', result)
    logging.info('Success: %s', result.get('success', 'N/A'))
    if result.get('output'):
        logging.info('Output: %s', result['output'])


if __name__ == "__main__":
    asyncio.run(main())