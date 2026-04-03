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

"""Host setup runner for the ``smolvm setup`` CLI command."""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SetupPlatform = Literal["linux", "macos"]

_LINUX_SCRIPT = "system-setup.sh"
_MACOS_SCRIPT = "system-setup-macos.sh"


@dataclass(frozen=True)
class SetupOptions:
    """Resolved CLI options for ``smolvm setup``."""

    check_only: bool = False
    with_docker: bool = False
    configure_runtime: bool = True
    skip_deps: bool = False
    runtime_user: str | None = None
    remove_runtime_config: bool = False


def packaged_asset_root() -> Path:
    """Return the packaged directory containing setup shell assets."""
    return Path(__file__).resolve().parent / "_setup_assets"


def detect_setup_platform(system_name: str | None = None) -> SetupPlatform:
    """Normalize the current OS into the supported setup platform names."""
    detected = system_name or platform.system()
    if detected == "Linux":
        return "linux"
    if detected == "Darwin":
        return "macos"
    raise RuntimeError(
        "`smolvm setup` is supported only on Linux and macOS. "
        f"Detected OS: {detected}."
    )


def resolve_setup_script(
    target: SetupPlatform,
    *,
    asset_root: Path | None = None,
) -> Path:
    """Resolve the packaged shell script for the requested platform."""
    root = asset_root or packaged_asset_root()
    script_name = _LINUX_SCRIPT if target == "linux" else _MACOS_SCRIPT
    script_path = root / script_name
    if script_path.is_file():
        return script_path
    raise FileNotFoundError(
        "Missing packaged setup asset.\n"
        f"Expected: {script_path}\n"
        "Reinstall smolvm or rebuild the wheel so setup assets are included."
    )


def build_setup_command(
    options: SetupOptions,
    *,
    system_name: str | None = None,
    asset_root: Path | None = None,
) -> list[str]:
    """Build the child ``bash ...`` invocation for ``smolvm setup``."""
    target = detect_setup_platform(system_name)
    script_path = resolve_setup_script(target, asset_root=asset_root)

    argv: list[str] = ["bash", str(script_path)]

    if target == "linux":
        if options.remove_runtime_config:
            argv.append("--remove-runtime-config")
            if options.runtime_user:
                argv.extend(["--runtime-user", options.runtime_user])
            return argv

        if options.check_only:
            argv.append("--check-only")
        if options.with_docker:
            argv.append("--with-docker")
        if options.configure_runtime:
            argv.append("--configure-runtime")
        if options.skip_deps:
            argv.append("--skip-deps")
        if options.runtime_user:
            argv.extend(["--runtime-user", options.runtime_user])
        return argv

    if options.check_only:
        argv.append("--check-only")
    if options.with_docker:
        argv.append("--with-docker")
    return argv


def run_setup(
    options: SetupOptions,
    *,
    system_name: str | None = None,
    asset_root: Path | None = None,
) -> int:
    """Execute the packaged setup script with inherited stdio."""
    command = build_setup_command(options, system_name=system_name, asset_root=asset_root)
    completed = subprocess.run(command, check=False)
    return int(completed.returncode)
