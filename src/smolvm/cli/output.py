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

"""Shared CLI output helpers for Rich and JSON rendering."""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text


def console_stdout() -> Console:
    """Return the Rich console configured for standard output."""
    return Console()


def console_stderr() -> Console:
    """Return the Rich console configured for standard error."""
    return Console(stderr=True)


def emit_json(
    command: str,
    exit_code: int,
    *,
    data: Any = None,
    error: dict[str, Any] | None = None,
) -> None:
    """Emit the unified JSON envelope to stdout."""
    normalized_error: dict[str, Any] | None = None
    if error is not None:
        normalized_error = {
            "code": error.get("code") or error.get("type") or "runtime_error",
            "message": error.get("message", ""),
        }
        if recovery := error.get("recovery"):
            normalized_error["recovery"] = recovery
        if "details" in error:
            normalized_error["details"] = error["details"]

    payload = {
        "ok": exit_code == 0 and normalized_error is None,
        "command": command,
        "exit_code": exit_code,
        "data": data,
        "error": normalized_error,
    }
    stdout = console_stdout().file
    stdout.write(json.dumps(payload, indent=2))
    stdout.write("\n")
    stdout.flush()


def emit_success(command: str, data: Any = None, *, exit_code: int = 0) -> int:
    """Emit a successful JSON envelope."""
    emit_json(command, exit_code, data=data)
    return exit_code


def emit_error(
    command: str,
    code: str,
    message: str,
    *,
    recovery: str | None = None,
    details: Any = None,
    exit_code: int = 1,
) -> int:
    """Emit a failed JSON envelope."""
    error: dict[str, Any] = {"code": code, "message": message}
    if recovery is not None:
        error["recovery"] = recovery
    if details is not None:
        error["details"] = details
    emit_json(command, exit_code, data=None, error=error)
    return exit_code


def render_error(message: str, hint: str | None = None) -> None:
    """Render a human-facing error panel to stderr."""
    body = Text(message)
    if hint:
        body.append("\n\n")
        body.append(f"Hint: {hint}", style="yellow")
    console_stderr().print(Panel.fit(body, title="Error", border_style="red"))


def render_empty(title: str, message: str) -> None:
    """Render a human-facing empty state panel to stdout."""
    console_stdout().print(Panel.fit(message, title=title, border_style="yellow"))


def status_style(status: str) -> str:
    """Return the Rich style to use for a status value."""
    normalized = status.lower()
    styles = {
        "pass": "bold green",
        "ok": "bold green",
        "running": "bold green",
        "deleted": "bold green",
        "success": "bold green",
        "warn": "bold yellow",
        "warning": "bold yellow",
        "stopped": "bold yellow",
        "paused": "bold blue",
        "fail": "bold red",
        "failed": "bold red",
        "error": "bold red",
        "created": "bold cyan",
        "dry-run": "bold cyan",
    }
    return styles.get(normalized, "white")
