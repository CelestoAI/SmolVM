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

"""Apply a preset to a running, SSH-ready SmolVM guest."""

from __future__ import annotations

import logging
import os
import shlex
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from smolvm.env import inject_env_vars
from smolvm.exceptions import SmolVMError

if TYPE_CHECKING:
    from smolvm.presets._types import Preset
    from smolvm.ssh import SSHClient


logger = logging.getLogger(__name__)

# How long the install script may take. Apt + NodeSource setup + global
# npm install can run for a couple of minutes on a cold image.
_DEFAULT_INSTALL_TIMEOUT = 600


def collect_host_env(preset: Preset) -> dict[str, str]:
    """Return env vars from os.environ that match preset.host_env_vars."""
    return {key: value for key in preset.host_env_vars if (value := os.environ.get(key))}


def apply_preset(
    ssh: SSHClient,
    preset: Preset,
    *,
    on_progress: Callable[[str], None] | None = None,
    install_timeout: int = _DEFAULT_INSTALL_TIMEOUT,
) -> dict[str, object]:
    """Apply *preset* to a guest reachable via *ssh*.

    Order: copy host configs, inject env vars, then run the install
    script. Configs are placed before install runs so that the freshly
    installed CLI finds its config on first invocation.

    Returns:
        A summary dict (copied paths, injected env keys) suitable for
        logging or JSON output.
    """
    notify = on_progress or (lambda _msg: None)

    copied_configs: list[str] = []
    for cfg in preset.host_configs:
        local = Path(cfg.host_path).expanduser()
        if not local.exists():
            if cfg.required:
                raise SmolVMError(
                    f"Required host config not found: {local}",
                    {"preset": preset.name, "host_path": str(local)},
                )
            logger.debug("Skipping missing host config: %s", local)
            continue
        notify(f"Copying {local} → {cfg.guest_path}")
        _copy_to_guest(ssh, local, cfg.guest_path)
        copied_configs.append(cfg.guest_path)

    env_vars = collect_host_env(preset)
    injected_keys: list[str] = []
    if env_vars:
        notify(f"Injecting {len(env_vars)} env var(s) from host")
        injected_keys = inject_env_vars(ssh, env_vars)

    if preset.install_script.strip():
        notify(f"Installing {preset.name} (this may take a minute)")
        result = ssh.run(preset.install_script, timeout=install_timeout)
        if not result.ok:
            stderr_tail = (result.stderr or "").strip().splitlines()[-20:]
            raise SmolVMError(
                f"Preset {preset.name!r} install failed (exit {result.exit_code})",
                {
                    "preset": preset.name,
                    "exit_code": result.exit_code,
                    "stderr_tail": "\n".join(stderr_tail),
                },
            )

    return {
        "preset": preset.name,
        "copied_configs": copied_configs,
        "injected_env_keys": injected_keys,
    }


def _copy_to_guest(ssh: SSHClient, local: Path, guest_path: str) -> None:
    """Copy a host file or directory tree to *guest_path* inside the VM."""
    if local.is_file():
        _copy_file(ssh, local, guest_path)
        return
    if local.is_dir():
        _copy_dir(ssh, local, guest_path)
        return
    raise SmolVMError(
        f"Unsupported host path type (not file or directory): {local}",
        {"host_path": str(local)},
    )


def _copy_file(ssh: SSHClient, local: Path, guest_path: str) -> None:
    """Upload a single host file to *guest_path* via SFTP, mkdir parent first."""
    parent = _posix_dirname(guest_path)
    if parent:
        result = ssh.run(f"mkdir -p -- {shlex.quote(parent)}", timeout=10)
        if not result.ok:
            raise SmolVMError(
                f"Failed to create guest directory {parent!r}",
                {"exit_code": result.exit_code, "stderr": result.stderr},
            )
    ssh.put_file(local, guest_path)


def _copy_dir(ssh: SSHClient, local: Path, guest_path: str) -> None:
    """Tar a host directory tree and untar it into *guest_path* on the guest."""
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        with tarfile.open(tmp_path, "w") as tf:
            tf.add(local, arcname=".")

        guest_tmp = f"/tmp/.smolvm-preset-{uuid4().hex}.tar"
        ssh.put_file(tmp_path, guest_tmp)
        cmd = (
            "set -e; "
            f"mkdir -p -- {shlex.quote(guest_path)}; "
            f"tar -xf {shlex.quote(guest_tmp)} -C {shlex.quote(guest_path)}; "
            f"rm -f -- {shlex.quote(guest_tmp)}"
        )
        result = ssh.run(cmd, timeout=60)
        if not result.ok:
            raise SmolVMError(
                f"Failed to extract config archive into {guest_path!r}",
                {"exit_code": result.exit_code, "stderr": result.stderr},
            )
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _posix_dirname(path: str) -> str:
    """Return the parent of a POSIX absolute path (no host-OS interpretation)."""
    if "/" not in path:
        return ""
    parent = path.rsplit("/", 1)[0]
    return parent or "/"


__all__ = ["apply_preset", "collect_host_env"]
