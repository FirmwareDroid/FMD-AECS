import asyncio
from llama_index.llms.ollama import Ollama
from droidrun import DroidAgent, AdbTools

async def main():
    # load adb tools for the first connected device
    tools = await AdbTools.create()

    # Set up the Ollama LLM with a modern model
    llm = Ollama(
        model="qwen2.5vl",  # or "gemma3", "deepseek", "llama4", etc.
        base_url="http://localhost:11434"  # default Ollama endpoint
    )

    # Create the DroidAgent
    agent = DroidAgent(
        goal="Open Settings and check battery level",
        llm=llm,
        tools=tools,
        vision=True,         # Optional: enable vision. use vision=False for deepseek models
        reasoning=True,       # Optional: enable planning/reasoning. Read more about the agent configuration in Core-Concepts/Agent
    )

    # Run the agent
    result = await agent.run()
    print(f"Success: {result['success']}")
    if result.get('output'):
        print(f"Output: {result['output']}")

if __name__ == "__main__":
    asyncio.run(main())
