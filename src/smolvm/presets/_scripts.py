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

"""Shared bash snippets used by built-in presets."""

from __future__ import annotations

import re

# npm package names. ``@scope/name`` is the only multi-segment form allowed.
_SAFE_NPM_NAME_RE = re.compile(r"^@?[a-zA-Z0-9._\-]+(/[a-zA-Z0-9._\-]+)?$")

# Wait for cloud-init to release the apt lock, install Node.js 20 from
# NodeSource if it's missing or older than 20. Idempotent.
NODE20_BOOTSTRAP = r"""
set -euo pipefail
cloud-init status --wait >/dev/null 2>&1 || true
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq --no-install-recommends curl ca-certificates gnupg

needs_node=1
if command -v node >/dev/null 2>&1; then
    major=$(node --version | sed 's/^v//' | cut -d. -f1)
    if [ "${major:-0}" -ge 20 ]; then
        needs_node=0
    fi
fi
if [ "$needs_node" = "1" ]; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y -qq --no-install-recommends nodejs
fi
"""


def npm_install_global(package: str) -> str:
    """Return a script that ensures Node 20+ and globally installs *package*."""
    if not _SAFE_NPM_NAME_RE.match(package):
        raise ValueError(f"Refusing to install unsafe npm package name: {package!r}")
    return NODE20_BOOTSTRAP + f"\nnpm install -g --silent {package}\n"
