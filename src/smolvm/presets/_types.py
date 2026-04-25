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

"""Type definitions for SmolVM agent-harness presets."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class HostConfigCopy:
    """A host file or directory to copy into the guest at preset apply time.

    Attributes:
        host_path: Source path on the host. Leading ``~`` is expanded.
        guest_path: Destination path inside the guest (absolute).
        required: If True, raise an error when the host path is missing.
            If False (default), the copy is silently skipped.
    """

    host_path: str
    guest_path: str
    required: bool = False


@dataclass(frozen=True)
class Preset:
    """A reusable agent-harness blueprint applied to a fresh sandbox.

    Presets describe a common pattern: boot an SSH-capable VM, copy a
    handful of host config files in, set a few env vars from the host
    environment, and run an idempotent install script over SSH.

    Attributes:
        name: CLI-facing identifier (e.g. ``"codex"``).
        summary: One-line description for ``--help`` output.
        install_script: Bash script run via ``ssh.run`` after boot.
            Should be idempotent and self-contained.
        host_env_vars: Names of host environment variables. Each one
            present and non-empty on the host is forwarded into the
            guest as a persistent env var.
        host_configs: Files or directories to copy from host to guest.
        default_mem_mib: Memory bump versus the OS default.
        default_disk_mib: Disk bump versus the OS default.
    """

    name: str
    summary: str
    install_script: str
    host_env_vars: tuple[str, ...] = field(default_factory=tuple)
    host_configs: tuple[HostConfigCopy, ...] = field(default_factory=tuple)
    default_mem_mib: int = 2048
    default_disk_mib: int = 8192
    launch_command: str | None = None
    """Guest command run when the user attaches after install — e.g. ``"codex"``."""
