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

"""smolvm update — upgrade SmolVM to the latest stable release from PyPI."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys

from smolvm.cli.output import console_stdout, emit_json
from smolvm.cli.version_check import (
    _fetch_latest_from_pypi,
    _get_current_version,
    _is_newer,
)


def _check_for_stable_update() -> tuple[str | None, str | None]:
    """Return ``(current, latest)`` when an upgrade is available, else ``(current, None)``.

    Never raises — any network failure silently returns ``(current, None)``.
    """
    current = _get_current_version()
    latest = _fetch_latest_from_pypi()
    if current is None or latest is None:
        return current, None
    if _is_newer(current, latest):
        return current, latest
    return current, None


def _is_uv_tool_install() -> bool:
    """Return True if smolvm was installed as a uv tool.

    Checks whether the running smolvm executable lives inside uv's tool
    bin directory, which is how ``uv tool install smolvm`` places it.
    """
    uv = shutil.which("uv")
    if uv is None:
        return False
    try:
        result = subprocess.run(
            [uv, "tool", "list"],
            capture_output=True,
            text=True,
        )
        return bool(re.search(r"^smolvm[ \t]", result.stdout, re.MULTILINE))
    except OSError:
        return False


def _run_upgrade(*, json_output: bool) -> tuple[int, str]:
    """Upgrade smolvm using the appropriate package manager and return ``(returncode, output)``.

    Detects whether smolvm was installed via ``uv tool`` or ``pip`` and
    calls the matching upgrade command. In terminal mode subprocess stdio
    is inherited so output streams live to the user. In JSON mode
    stdout+stderr are captured for embedding in the response payload.
    """
    if _is_uv_tool_install():
        uv = shutil.which("uv") or "uv"
        cmd = [uv, "tool", "upgrade", "smolvm"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "smolvm"]

    try:
        if json_output:
            result = subprocess.run(cmd, capture_output=True, text=True)
            output = result.stdout + result.stderr
            return result.returncode, output
        else:
            result = subprocess.run(cmd, text=True)
            return result.returncode, ""
    except OSError as exc:
        if not json_output:
            sys.stderr.write(f"Upgrade failed: {exc}\n")
        return 1, str(exc)


def run_update(*, check: bool = False, json_output: bool = False) -> int:
    """Execute ``smolvm update``."""
    current, latest = _check_for_stable_update()

    if check:
        if latest is None:
            if current is None:
                data: dict[str, object] = {
                    "current": None,
                    "latest": None,
                    "update_available": False,
                }
                if json_output:
                    emit_json("update", 1, data=data)
                else:
                    sys.stderr.write(
                        "Could not determine the installed smolvm version. "
                        "Run: pip install --upgrade smolvm\n"
                    )
                return 1
            data = {"current": current, "latest": None, "update_available": False}
            if json_output:
                emit_json("update", 0, data=data)
            else:
                console = console_stdout()
                console.print(f"smolvm {current} is up to date.")
        else:
            data = {"current": current, "latest": latest, "update_available": True}
            if json_output:
                emit_json("update", 0, data=data)
            else:
                console = console_stdout()
                console.print(
                    f"Update available: {current} → {latest}. "
                    f"Run [bold]smolvm update[/bold] to install."
                )
        return 0

    if latest is None and current is not None:
        if json_output:
            emit_json(
                "update",
                0,
                data={"current": current, "latest": current, "upgraded": False},
            )
        else:
            console = console_stdout()
            console.print(f"smolvm {current} is already the latest stable release.")
        return 0

    if not json_output:
        console = console_stdout()
        if latest:
            console.print(f"Upgrading smolvm {current} → {latest} …")
        else:
            console.print("Upgrading smolvm to the latest stable release …")

    returncode, pip_output = _run_upgrade(json_output=json_output)

    if json_output:
        new_version = _get_current_version()
        emit_json(
            "update",
            returncode,
            data={
                "previous": current,
                "current": new_version,
                "upgraded": returncode == 0,
                "pip_output": pip_output,
            },
        )
        return returncode

    if returncode != 0:
        sys.stderr.write("smolvm update failed. To retry, run: pip install --upgrade smolvm\n")
    return returncode
