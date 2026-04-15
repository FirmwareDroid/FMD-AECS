import asyncio
import os
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

    print(f"--- Results ---")
    print(f"Result object: {result}")
    print(f"Success: {result.get('success', 'N/A')}")
    if result.get('output'):
        print(f"Output: {result['output']}")


if __name__ == "__main__":
    asyncio.run(main())