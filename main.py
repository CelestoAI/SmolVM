from agentor import Agentor
from agentor.runtime import SmolVMRuntime
from agentor.tools import ShellTool


def main() -> None:
    runtime = SmolVMRuntime(mem_size_mib=1024, disk_size_mib=2048)

    try:
        agent = Agentor(
            name="SmolVM Shell Agent",
            model="gpt-5",
            tools=[ShellTool(executor=runtime)],
            instructions="Use shell commands to inspect files inside the SmolVM sandbox.",
        )

        result = agent.run(
            "Install uv and run use Python interpreter to print 'Hello, World!'. Return both outputs."
        )
        print(result)
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
