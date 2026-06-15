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
from importlib.resources import files
from os import fspath
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
    for_bake: bool = False
    skip_kvm_check: bool = False
    skip_runtime_check: bool = False
    firecracker_version: str | None = None


def packaged_asset_root() -> Path:
    """Return the directory containing setup shell assets.

    Checks the installed ``smolvm._setup_assets`` package first, then falls
    back to the repository ``scripts/`` directory so ``uv run smolvm setup``
    works from a source checkout.
    """
    pkg_dir = _package_asset_root()
    if pkg_dir is not None and (pkg_dir / _LINUX_SCRIPT).is_file():
        return pkg_dir

    # In a source checkout, the installable resource package exists but does
    # not contain the shell scripts. Fall back to the repo-root scripts/ dir:
    # src/smolvm/host/setup.py → src/smolvm/host → src/smolvm → src → repo root.
    repo_scripts = Path(__file__).resolve().parents[3] / "scripts"
    if (repo_scripts / _LINUX_SCRIPT).is_file():
        return repo_scripts

    # Preserve the installed package path in error messages when the wheel is
    # present but incomplete; otherwise return the repo fallback candidate.
    return pkg_dir or repo_scripts


def _package_asset_root() -> Path | None:
    """Return the installed setup asset directory when it is filesystem-backed."""
    try:
        return Path(fspath(files("smolvm._setup_assets")))
    except (ModuleNotFoundError, TypeError):
        return None


def detect_setup_platform(system_name: str | None = None) -> SetupPlatform:
    """Normalize the current OS into the supported setup platform names."""
    detected = system_name or platform.system()
    if detected == "Linux":
        return "linux"
    if detected == "Darwin":
        return "macos"
    raise RuntimeError(
        f"`smolvm setup` is supported only on Linux and macOS. Detected OS: {detected}."
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
        if options.for_bake:
            argv.append("--for-bake")
        if options.skip_kvm_check and not options.for_bake:
            argv.append("--skip-kvm-check")
        if options.skip_runtime_check and not options.for_bake:
            argv.append("--skip-runtime-check")
        if options.firecracker_version:
            argv.extend(["--firecracker-version", options.firecracker_version])
        return argv

    if options.check_only:
        argv.append("--check-only")
    if options.with_docker:
        argv.append("--with-docker")
    if options.skip_deps:
        argv.append("--skip-deps")
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
