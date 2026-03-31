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

"""Drive a SmolVM browser from PydanticAI through agent-browser.

Prerequisites:
    pip install smolvm pydantic-ai
    brew install agent-browser or npm install -g agent-browser
    agent-browser install
    export OPENAI_API_KEY=...
    smolvm doctor

Example:
    python examples/agent_tools/pydanticai_agent_browser.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
from pydantic_ai import Agent

if TYPE_CHECKING:
    from pydantic_ai import RunContext

DEFAULT_MODEL = "openai:gpt-4.1"
REPO_ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS_DIR = REPO_ROOT / "artifacts" / "pydanticai-agent-browser"
FINAL_SCREENSHOT_PATH = "artifacts/pydanticai-agent-browser/final.png"
SYSTEM_INSTRUCTIONS = (
    "You automate one SmolVM browser session from the host.\n"
    "Follow this workflow exactly:\n"
    "1. First run `smolvm browser start --live --json`.\n"
    "2. Read `cdp_port` from the `parsed_browser_session` section in the tool output.\n"
    "3. Use `agent-browser --cdp <cdp_port>` on every browser command.\n"
    "4. Use `agent-browser --cdp <cdp_port> snapshot -i --json` before choosing refs.\n"
    f"5. Save the final screenshot to `{FINAL_SCREENSHOT_PATH}`.\n"
    "6. Stop the browser with `smolvm browser stop <session_id>` when done.\n"
    "7. Return only these four lines: title, url, screenshot_path, session_id.\n"
    "Only use the `run_host_bash` tool, and keep each command simple."
)
DEMO_PROMPT = (
    "Use run_host_bash to complete this exact demo:\n"
    "1. Open https://celesto.ai\n"
    "2. Capture a snapshot\n"
    "3. Click the `Get started...` link\n"
    "4. Wait for the destination page to load\n"
    "5. Capture another snapshot\n"
    "6. Scroll down a bit\n"
    f"7. Save a screenshot to `{FINAL_SCREENSHOT_PATH}`\n"
    "8. Fetch the current title and URL\n"
    "9. Stop the session\n"
    "Return only the final title, URL, page summary, screenshot path, and session ID."
)


@dataclass
class BrowserCliDeps:
    """Hold the active browser session so cleanup can happen outside the agent."""

    session: dict[str, Any] | None = None


def _parse_browser_start_output(stdout: str) -> dict[str, Any] | None:
    """Parse `smolvm browser start --json` output into a simple dictionary."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None

    data = payload.get("data")
    session_id = data.get("session_id") if isinstance(data, dict) else None
    if not isinstance(session_id, str) or not session_id:
        return None

    cdp_url = data.get("cdp_url")
    if not isinstance(cdp_url, str) or not cdp_url:
        raise RuntimeError(
            "SmolVM returned browser session JSON without a usable cdp_url. "
            "This example expects `smolvm browser start --json` to include it."
        )

    parsed = urlparse(cdp_url)
    try:
        cdp_port = parsed.port
    except ValueError as exc:
        raise RuntimeError(
            "SmolVM returned an unexpected browser cdp_url. "
            "This example needs a localhost port for `agent-browser --cdp`."
        ) from exc
    if cdp_port is None:
        raise RuntimeError(
            "SmolVM returned an unexpected browser cdp_url without a port. "
            "This example needs that port for `agent-browser --cdp`."
        )

    live_url = data.get("live_url")
    artifacts_dir = data.get("artifacts_dir")
    return {
        "session_id": session_id,
        "cdp_url": cdp_url,
        "cdp_port": cdp_port,
        "live_url": live_url if isinstance(live_url, str) and live_url else None,
        "artifacts_dir": (
            artifacts_dir if isinstance(artifacts_dir, str) and artifacts_dir else None
        ),
    }


def _format_command_result(
    exit_code: int,
    stdout: str,
    stderr: str,
    parsed_browser_session: dict[str, Any] | None = None,
) -> str:
    """Return a plain-text command summary for the agent."""
    lines = [
        f"exit_code: {exit_code}",
        "stdout:",
        stdout.strip() or "<empty>",
        "stderr:",
        stderr.strip() or "<empty>",
    ]
    if parsed_browser_session is not None:
        lines.extend(
            [
                "parsed_browser_session:",
                f"session_id: {parsed_browser_session['session_id']}",
                f"cdp_url: {parsed_browser_session['cdp_url']}",
                f"cdp_port: {parsed_browser_session['cdp_port']}",
                f"live_url: {parsed_browser_session.get('live_url') or '<none>'}",
                f"artifacts_dir: {parsed_browser_session.get('artifacts_dir') or '<none>'}",
            ]
        )
    return "\n".join(lines)


def run_host_bash(
    ctx: RunContext[BrowserCliDeps],
    command: str,
    timeout: int = 60,
) -> str:
    """Run a host-side bash command from the repository root.

    Args:
        command: Shell command to execute on the host.
        timeout: Maximum number of seconds to wait for the command.
    """
    try:
        result = subprocess.run(
            ["bash", "-lc", command],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        timeout_message = f"Command timed out after {timeout} seconds."
        if stderr:
            stderr = f"{timeout_message}\n{stderr}"
        else:
            stderr = timeout_message
        return _format_command_result(124, stdout, stderr)

    parsed_browser_session: dict[str, Any] | None = None
    if (
        result.returncode == 0
        and command.strip().startswith("smolvm browser start")
        and "--json" in command
    ):
        parsed_browser_session = _parse_browser_start_output(result.stdout)
        if parsed_browser_session is not None:
            ctx.deps.session = parsed_browser_session

    return _format_command_result(
        result.returncode,
        result.stdout,
        result.stderr,
        parsed_browser_session=parsed_browser_session,
    )


def _build_agent() -> Any:
    agent = Agent(
        os.environ.get("PYDANTICAI_MODEL", DEFAULT_MODEL),
        deps_type=BrowserCliDeps,
        instructions=SYSTEM_INSTRUCTIONS,
    )
    agent.tool(docstring_format="google", require_parameter_descriptions=True)(run_host_bash)
    return agent


def main() -> None:
    """Run the minimal SmolVM plus agent-browser PydanticAI demo."""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

    agent = _build_agent()
    deps = BrowserCliDeps()
    try:
        result = agent.run_sync(DEMO_PROMPT, deps=deps)
        print(result.output)
        if deps.session is not None and deps.session.get("live_url"):
            print(f"live_url: {deps.session['live_url']}")
        if deps.session is not None and deps.session.get("artifacts_dir"):
            print(f"artifacts_dir: {deps.session['artifacts_dir']}")
    finally:
        if deps.session is not None:
            stop_result = subprocess.run(
                ["smolvm", "browser", "stop", str(deps.session["session_id"])],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if stop_result.returncode != 0 and "not found" not in stop_result.stderr.lower():
                error_message = stop_result.stderr.strip() or (
                    f"Failed to stop browser session {deps.session['session_id']}."
                )
                print(error_message, file=sys.stderr)
            deps.session = None


if __name__ == "__main__":
    main()
