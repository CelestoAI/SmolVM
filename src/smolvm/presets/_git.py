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

"""Host git credentials copied into every preset's sandbox.

Coding agents need ``git``, ``gh``, and ``ssh git@github.com`` to work
without re-auth. The applier in ``_install.py`` runs these copies
through the same pipeline as harness-specific configs (e.g.
``~/.codex``); missing entries are skipped silently. The whole
``~/.ssh`` directory rides through ``_copy_dir`` so tar preserves
0o600 modes — sshd would reject world-readable keys otherwise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from smolvm.exceptions import SmolVMError
from smolvm.presets._types import HostConfigCopy

if TYPE_CHECKING:
    from smolvm.ssh import SSHClient

GIT_HOST_CONFIGS: tuple[HostConfigCopy, ...] = (
    HostConfigCopy(host_path="~/.gitconfig", guest_path="/root/.gitconfig"),
    HostConfigCopy(host_path="~/.config/git/config", guest_path="/root/.config/git/config"),
    # ~/.git-credentials holds plaintext "https://user:token@host" lines;
    # SFTP would otherwise drop it at the server's umask (typically 0644).
    HostConfigCopy(
        host_path="~/.git-credentials",
        guest_path="/root/.git-credentials",
        file_mode=0o600,
    ),
    HostConfigCopy(host_path="~/.ssh", guest_path="/root/.ssh"),
    HostConfigCopy(host_path="~/.config/gh", guest_path="/root/.config/gh"),
)

# Workspace mounts ride a virtio-9p share, which preserves the host's
# uid/gid (e.g. macOS 501:20). Inside the guest we run as root (uid 0),
# so git's CVE-2022-24765 ownership check fires on every command with
# "fatal: detected dubious ownership". Pre-register these paths as safe
# in the guest's global git config. Two patterns cover both shapes:
#   /workspace*    — the mount point itself (single mount at /workspace
#                    or numbered mounts at /workspace-1, /workspace-2…)
#   /workspace*/** — repos nested below the mount point
# Wildcards in safe.directory require git ≥ 2.43, which Ubuntu Noble
# (the only image we boot) ships. Scoped intentionally — the trust
# scope matches the existing one (anyone with sandbox shell is already
# trusted), but limiting to /workspace avoids vouching for paths the
# user never asked us to share.
WORKSPACE_SAFE_DIRECTORIES: tuple[str, ...] = ("/workspace*", "/workspace*/**")


def register_workspace_safe_directories(ssh: SSHClient) -> None:
    """Add WORKSPACE_SAFE_DIRECTORIES to the guest's global git config.

    Idempotent: uses ``--replace-all`` per path with a value-regex that
    matches only that exact path, so re-runs collapse to one entry per
    path instead of appending duplicates, and any unrelated
    ``safe.directory`` entries the user's host gitconfig brought along
    are preserved.
    """
    parts: list[str] = []
    for path in WORKSPACE_SAFE_DIRECTORIES:
        # WORKSPACE_SAFE_DIRECTORIES values contain only `/`, letters,
        # and `*` (the only regex metachar), so escaping by hand is
        # safe and clearer than pulling in `re.escape`.
        regex = "^" + path.replace("*", r"\*") + "$"
        parts.append(
            f"git config --global --replace-all safe.directory '{path}' '{regex}'"
        )
    cmd = " && ".join(parts)
    result = ssh.run(cmd, timeout=10)
    if not result.ok:
        raise SmolVMError(
            "Failed to register workspace safe.directory entries on guest",
            {"exit_code": result.exit_code, "stderr": result.stderr},
        )


__all__ = [
    "GIT_HOST_CONFIGS",
    "WORKSPACE_SAFE_DIRECTORIES",
    "register_workspace_safe_directories",
]
