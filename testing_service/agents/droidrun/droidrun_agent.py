import asyncio
import os
from dotenv import load_dotenv
from llama_index.llms.openai_like import OpenAILike
from droidrun import DroidAgent, AdbTools

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

    # Create the DroidAgent
    agent = DroidAgent(
        llms=llm,  # Move LLM to the first position
        goal="Open Settings and check battery level",
        tools=tools,
    )

    # Run the agent
    result = await agent.run()

    print(f"--- Results ---")
    print(f"Success: {result['success']}")
    if result.get('output'):
        print(f"Output: {result['output']}")


if __name__ == "__main__":
    asyncio.run(main())