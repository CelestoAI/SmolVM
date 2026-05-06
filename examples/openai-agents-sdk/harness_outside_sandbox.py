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

from agents import ModelSettings, Runner
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.entries import File
from celesto.integrations.openai_agents import SmolVMSandboxClient, SmolVMSandboxClientOptions

DEFAULT_MODEL = "gpt-5.5"


def _build_manifest() -> Manifest:
    """Create the files the agent will see inside the sandbox."""
    return Manifest(
        entries={
            "customer_brief.md": File(
                content=(
                    b"# Northwind Health renewal\n\n"
                    b"- Segment: Mid-market healthcare analytics provider.\n"
                    b"- Renewal date: 2026-04-15.\n"
                    b"- Target outcome: close the renewal this month.\n"
                )
            ),
            "implementation_risks.md": File(
                content=(
                    b"# Delivery risks\n\n"
                    b"- Security questionnaire is not complete.\n"
                    b"- Procurement needs final legal language by April 1.\n"
                    b"- The customer asked for a clear owner for onboarding.\n"
                )
            ),
            "task.md": File(
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
    manifest = _build_manifest()
    client = SmolVMSandboxClient()
    session = await client.create(
        manifest=manifest,
        options=SmolVMSandboxClientOptions(
            os="ubuntu",
            memory=1024,
        ),
    )

    agent = SandboxAgent(
        name="SmolVM Renewal Analyst",
        model=os.environ.get("OPENAI_AGENTS_MODEL", DEFAULT_MODEL),
        instructions=(
            "Inspect the sandbox files before answering. "
            "Use the sandbox tools to read the files. "
            "Write your Markdown report to output/renewal_summary.md. "
            "Keep the final response short and mention that file path."
        ),
        default_manifest=manifest,
        model_settings=ModelSettings(tool_choice="required"),
    )

    try:
        async with session:
            print(f"Sandbox ready: {session.state.vm_id}")
            print("\n== Initial sandbox files ==")
            print(await session.ls("."))

            result = await Runner.run(
                agent,
                "Summarize the renewal blockers and recommend the next two actions.",
                run_config=RunConfig(
                    sandbox=SandboxRunConfig(session=session),
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
