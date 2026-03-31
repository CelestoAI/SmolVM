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

"""Use SmolVM as a reusable PydanticAI tool-backed sandbox.

Install:
    pip install smolvm pydantic-ai

Required environment:
    export OPENAI_API_KEY=...

Optional environment:
    export PYDANTICAI_MODEL=openai:gpt-4.1

Before running:
    smolvm doctor

Example:
    python examples/agent_tools/pydanticai_reusable_tool.py
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from smolvm import SmolVM

try:
    from pydantic_ai import RunContext
except ImportError:
    if TYPE_CHECKING:
        raise

    class RunContext:
        """Fallback used so runtime annotation evaluation does not fail."""

        def __class_getitem__(cls, _item: Any) -> type["RunContext"]:
            return cls

DEFAULT_MODEL = "openai:gpt-4.1"


@dataclass
class SandboxDeps:
    """Host-side dependency container for a reusable SmolVM instance."""

    vm_id: str | None = None


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


def _connect_vm(deps: SandboxDeps) -> SmolVM:
    """Return the active sandbox, creating it on first use."""
    if deps.vm_id is None:
        vm = SmolVM()
        vm.start()
        deps.vm_id = vm.vm_id
        return vm
    return SmolVM.from_id(deps.vm_id)


def _cleanup_vm(vm_id: str | None) -> None:
    """Delete the reusable sandbox if one was created."""
    if vm_id is None:
        return

    vm = SmolVM.from_id(vm_id)
    try:
        vm.delete()
    finally:
        vm.close()


def run_in_reusable_smolvm(
    ctx: RunContext[SandboxDeps],
    command: str,
    timeout: int = 30,
) -> str:
    """Run a shell command inside a reusable SmolVM sandbox.

    Args:
        command: Shell command to execute inside the sandbox guest.
        timeout: Maximum number of seconds to wait for the command.
    """
    vm = _connect_vm(ctx.deps)
    try:
        result = vm.run(command, timeout=timeout)
        return _format_command_result(result.exit_code, result.stdout, result.stderr)
    finally:
        vm.close()


def _build_agent() -> Any:
    agent_cls = _require_dependency("pydantic_ai:Agent", "pip install pydantic-ai")
    agent = agent_cls(
        os.environ.get("PYDANTICAI_MODEL", DEFAULT_MODEL),
        deps_type=SandboxDeps,
        instructions=(
            "You are a coding assistant with access to a reusable SmolVM sandbox. "
            "For shell and file-system tasks, call run_in_reusable_smolvm exactly "
            "once and then summarize the result."
        ),
    )
    agent.tool(docstring_format="google", require_parameter_descriptions=True)(
        run_in_reusable_smolvm
    )
    return agent


def main() -> None:
    """Run two turns against the same SmolVM-backed sandbox."""
    agent = _build_agent()
    deps = SandboxDeps()
    try:
        first_prompt = (
            "Use run_in_reusable_smolvm to run this exact command inside the sandbox: "
            "`printf 'persistent state from SmolVM\\n' > /tmp/agent-note.txt && "
            "cat /tmp/agent-note.txt`. Then summarize what happened."
        )
        first_result = agent.run_sync(first_prompt, deps=deps)
        print("First run:")
        print(first_result.output)
        print(f"Reusable VM ID: {deps.vm_id}")

        second_prompt = (
            "Use run_in_reusable_smolvm to run this exact command inside the sandbox: "
            "`cat /tmp/agent-note.txt`. Then confirm that the file still exists."
        )
        second_result = agent.run_sync(second_prompt, deps=deps)
        print("\nSecond run:")
        print(second_result.output)
        print(f"Reusable VM ID: {deps.vm_id}")
    finally:
        _cleanup_vm(deps.vm_id)


if __name__ == "__main__":
    main()
