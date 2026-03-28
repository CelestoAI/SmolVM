#!/usr/bin/env python3

# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Use SmolVM as an OpenAI Agents SDK function tool.

Install:
    pip install smolvm openai-agents

Required environment:
    export OPENAI_API_KEY=...

Optional environment:
    export OPENAI_AGENTS_MODEL=gpt-4.1

Before running:
    smolvm doctor

Example:
    python examples/agent_tools/openai_agents_tool.py
"""

from __future__ import annotations

import asyncio
import os

from agents import Agent, Runner, function_tool

from smolvm import SmolVM

DEFAULT_MODEL = "gpt-4.1"


def _format_command_result(exit_code: int, stdout: str, stderr: str) -> str:
    """Return a plain-text command summary for the agent."""
    return (
        f"exit_code: {exit_code}\n"
        f"stdout:\n{stdout.strip() or '<empty>'}\n"
        f"stderr:\n{stderr.strip() or '<empty>'}"
    )


@function_tool
def run_in_smolvm(command: str, timeout: int = 30) -> str:
    """Run a shell command inside an ephemeral SmolVM sandbox.

    Args:
        command: Shell command to execute inside the sandbox guest.
        timeout: Maximum number of seconds to wait for the command.
    """
    with SmolVM() as vm:
        result = vm.run(command, timeout=timeout)
        return _format_command_result(result.exit_code, result.stdout, result.stderr)


async def main() -> None:
    """Run a minimal OpenAI Agents SDK example with SmolVM as a tool."""
    agent = Agent(
        name="SmolVM Assistant",
        model=os.environ.get("OPENAI_AGENTS_MODEL", DEFAULT_MODEL),
        instructions=(
            "You are a coding assistant with access to a secure SmolVM sandbox. "
            "For shell or Python inspection requests, call run_in_smolvm exactly "
            "once and then summarize the result."
        ),
        tools=[run_in_smolvm],
    )
    prompt = (
        "Use run_in_smolvm to run this exact command inside the sandbox: "
        "`uname -a && python3 --version`. Then summarize what you found."
    )
    result = await Runner.run(agent, prompt)
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
