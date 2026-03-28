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

"""Use SmolVM as a PydanticAI stateless tool.

Install:
    pip install smolvm pydantic-ai

Required environment:
    export OPENAI_API_KEY=...

Optional environment:
    export PYDANTICAI_MODEL=openai:gpt-4.1

Before running:
    smolvm doctor

Example:
    python examples/agent_tools/pydanticai_tool.py
"""

from __future__ import annotations

import os
from typing import Any

from smolvm import SmolVM

DEFAULT_MODEL = "openai:gpt-4.1"


def _require_dependency(import_path: str, install_hint: str) -> Any:
    """Import an optional dependency lazily with a useful installation hint."""
    module_name, _, attr_name = import_path.partition(":")
    try:
        module = __import__(module_name, fromlist=[attr_name] if attr_name else [])
    except ImportError as exc:
        raise RuntimeError(
            f"Missing dependency '{module_name}'. Install it with: {install_hint}"
        ) from exc
    return getattr(module, attr_name) if attr_name else module


def _format_command_result(exit_code: int, stdout: str, stderr: str) -> str:
    """Return a plain-text command summary for the agent."""
    return (
        f"exit_code: {exit_code}\n"
        f"stdout:\n{stdout.strip() or '<empty>'}\n"
        f"stderr:\n{stderr.strip() or '<empty>'}"
    )


def run_in_smolvm(command: str, timeout: int = 30) -> str:
    """Run a shell command inside an ephemeral SmolVM sandbox.

    Args:
        command: Shell command to execute inside the sandbox guest.
        timeout: Maximum number of seconds to wait for the command.
    """
    with SmolVM() as vm:
        result = vm.run(command, timeout=timeout)
        return _format_command_result(result.exit_code, result.stdout, result.stderr)


def _build_agent() -> Any:
    agent_cls = _require_dependency("pydantic_ai:Agent", "pip install pydantic-ai")
    agent = agent_cls(
        os.environ.get("PYDANTICAI_MODEL", DEFAULT_MODEL),
        instructions=(
            "You are a coding assistant with access to a secure SmolVM sandbox. "
            "For shell or Python inspection requests, call run_in_smolvm exactly "
            "once and then summarize the result."
        ),
    )
    agent.tool_plain(docstring_format="google", require_parameter_descriptions=True)(
        run_in_smolvm
    )
    return agent


def main() -> None:
    """Run a minimal PydanticAI example with SmolVM as a tool."""
    agent = _build_agent()
    prompt = (
        "Use run_in_smolvm to run this exact command inside the sandbox: "
        "`uname -a && python3 --version`. Then summarize what you found."
    )
    result = agent.run_sync(prompt)
    print(result.output)


if __name__ == "__main__":
    main()
