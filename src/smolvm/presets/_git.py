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

from smolvm.presets._types import HostConfigCopy

GIT_HOST_CONFIGS: tuple[HostConfigCopy, ...] = (
    HostConfigCopy(host_path="~/.gitconfig", guest_path="/root/.gitconfig"),
    HostConfigCopy(host_path="~/.config/git/config", guest_path="/root/.config/git/config"),
    HostConfigCopy(host_path="~/.git-credentials", guest_path="/root/.git-credentials"),
    HostConfigCopy(host_path="~/.ssh", guest_path="/root/.ssh"),
    HostConfigCopy(host_path="~/.config/gh", guest_path="/root/.config/gh"),
)


__all__ = ["GIT_HOST_CONFIGS"]
