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

"""Give an OpenAI agent a SmolVM sandbox.

This example gives an OpenAI agent its own local computer for work. The agent
can inspect files, run commands, and write a report without running those
commands on your machine.

The SmolVM provider lives in the Celesto SDK. This example shows how SmolVM
users can consume that provider instead of carrying provider code in every app.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "gpt-5.5"
INSTALL_HINT = "pip install celesto openai-agents"


def _require_dependency(import_path: str, install_hint: str = INSTALL_HINT) -> Any:
    """Import an optional dependency and show the install command if it is missing."""
    module_name, _, attr_name = import_path.partition(":")
    try:
        module = __import__(module_name, fromlist=[attr_name] if attr_name else [])
    except ImportError as exc:
        raise RuntimeError(
            f"This example needs an extra package. Run `{install_hint}` and try again."
        ) from exc
    return getattr(module, attr_name) if attr_name else module


def _build_manifest() -> Any:
    """Create the files the agent will see inside the sandbox."""
    manifest_cls = _require_dependency("agents.sandbox:Manifest")
    file_cls = _require_dependency("agents.sandbox.entries:File")

    return manifest_cls(
        entries={
            "customer_brief.md": file_cls(
                content=(
                    b"# Northwind Health renewal\n\n"
                    b"- Segment: Mid-market healthcare analytics provider.\n"
                    b"- Renewal date: 2026-04-15.\n"
                    b"- Target outcome: close the renewal this month.\n"
                )
            ),
            "implementation_risks.md": file_cls(
                content=(
                    b"# Delivery risks\n\n"
                    b"- Security questionnaire is not complete.\n"
                    b"- Procurement needs final legal language by April 1.\n"
                    b"- The customer asked for a clear owner for onboarding.\n"
                )
            ),
            "task.md": file_cls(
                content=(
                    b"# Task\n\n"
                    b"Review the workspace and write `output/renewal_summary.md`.\n"
                    b"The summary should have a title, blockers, and next actions.\n"
                )
            ),
        }
    )


async def main() -> None:
    """Run one OpenAI agent task in a SmolVM sandbox."""
    runner_cls = _require_dependency("agents:Runner")
    model_settings_cls = _require_dependency("agents:ModelSettings")
    run_config_cls = _require_dependency("agents.run:RunConfig")
    sandbox_agent_cls = _require_dependency("agents.sandbox:SandboxAgent")
    sandbox_run_config_cls = _require_dependency("agents.sandbox:SandboxRunConfig")
    smolvm_sandbox_client_cls = _require_dependency(
        "celesto.integrations.openai_agents:SmolVMSandboxClient"
    )
    smolvm_sandbox_client_options_cls = _require_dependency(
        "celesto.integrations.openai_agents:SmolVMSandboxClientOptions"
    )

    manifest = _build_manifest()
    client = smolvm_sandbox_client_cls()
    session = await client.create(
        manifest=manifest,
        options=smolvm_sandbox_client_options_cls(
            os="ubuntu",
            memory=1024,
        ),
    )

    agent = sandbox_agent_cls(
        name="SmolVM Renewal Analyst",
        model=os.environ.get("OPENAI_AGENTS_MODEL", DEFAULT_MODEL),
        instructions=(
            "Inspect the sandbox files before answering. "
            "Use the sandbox tools to read the files. "
            "Write your Markdown report to output/renewal_summary.md. "
            "Keep the final response short and mention that file path."
        ),
        default_manifest=manifest,
        model_settings=model_settings_cls(tool_choice="required"),
    )

    try:
        async with session:
            print(f"Sandbox ready: {session.state.vm_id}")
            print("\n== Initial sandbox files ==")
            print(await session.ls("."))

            result = await runner_cls.run(
                agent,
                "Summarize the renewal blockers and recommend the next two actions.",
                run_config=run_config_cls(
                    sandbox=sandbox_run_config_cls(session=session),
                    workflow_name="SmolVM SandboxAgent tutorial",
                ),
            )

            print("\n== Assistant summary ==")
            print(result.final_output)

            print("\n== Report written in the sandbox ==")
            artifact = await session.read(Path("output/renewal_summary.md"))
            try:
                payload = artifact.read()
            finally:
                artifact.close()
            if isinstance(payload, bytes):
                print(payload.decode("utf-8", errors="replace").strip())
            else:
                print(str(payload).strip())
    finally:
        await client.delete(session)


if __name__ == "__main__":
    asyncio.run(main())
