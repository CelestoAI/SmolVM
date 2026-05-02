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

# PyPI package names — alphanumerics, hyphens, underscores, dots, optional extras.
_SAFE_PYPI_NAME_RE = re.compile(r"^[a-zA-Z0-9._\-]+(\[[a-zA-Z0-9,._\-]+\])?$")


def node_bootstrap(major: int = 20) -> str:
    """Return a bash script that installs Node.js *major* via NodeSource.

    Waits for cloud-init to release the apt lock, then installs Node
    from NodeSource if it is missing or older than *major*. Idempotent.
    """
    if not isinstance(major, int) or major < 16:
        raise ValueError(f"Unsupported Node major version: {major}")
    return rf"""
set -euo pipefail
cloud-init status --wait >/dev/null 2>&1 || true
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq --no-install-recommends curl ca-certificates gnupg git

needs_node=1
if command -v node >/dev/null 2>&1; then
    major=$(node --version | sed 's/^v//' | cut -d. -f1)
    if [ "${{major:-0}}" -ge {major} ]; then
        needs_node=0
    fi
fi
if [ "$needs_node" = "1" ]; then
    curl -fsSL https://deb.nodesource.com/setup_{major}.x | bash -
    apt-get install -y -qq --no-install-recommends nodejs
fi
"""


# Backward-compatible constant used by existing presets.
NODE20_BOOTSTRAP = node_bootstrap(20)

PYTHON_BOOTSTRAP = r"""
set -euo pipefail
cloud-init status --wait >/dev/null 2>&1 || true
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    python3 python3-pip python3-venv curl ca-certificates git

export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
"""


def npm_install_global(package: str) -> str:
    """Return a script that globally installs *package* via npm.

    Assumes Node is already on PATH — pair this with
    :data:`NODE20_BOOTSTRAP` as the preset's ``setup_script`` so that
    the apt/Node phase and the npm phase show up as two separate
    progress steps in the CLI.
    """
    if not _SAFE_NPM_NAME_RE.match(package):
        raise ValueError(f"Refusing to install unsafe npm package name: {package!r}")
    return f"set -euo pipefail\nnpm install -g --silent {package}\n"


def uv_install_global(package: str) -> str:
    """Return a script that installs *package* system-wide via ``uv pip``.

    Pair with :data:`PYTHON_BOOTSTRAP` as the preset's ``setup_script``
    so that the apt/uv phase and the pip-install phase show up as two
    separate progress steps in the CLI.
    """
    if not _SAFE_PYPI_NAME_RE.match(package):
        raise ValueError(f"Refusing to install unsafe PyPI package name: {package!r}")
    return (
        "set -euo pipefail\n"
        'export PATH="$HOME/.local/bin:$PATH"\n'
        f"uv pip install --system {package}\n"
    )
