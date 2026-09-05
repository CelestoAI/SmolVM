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
_SAFE_NPM_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){2}(?:[-+][a-zA-Z0-9._-]+)?$")
_SAFE_EXECUTABLE_RE = re.compile(r"^[a-zA-Z0-9._\-]+$")

# PyPI package names — alphanumerics, hyphens, underscores, dots, optional extras.
_SAFE_PYPI_NAME_RE = re.compile(r"^[a-zA-Z0-9._\-]+(\[[a-zA-Z0-9,._\-]+\])?$")


def node_bootstrap(
    major: int = 20,
    *,
    minimum_version: tuple[int, int, int] | None = None,
) -> str:
    """Return a bash script that installs Node.js *major* on Ubuntu or Alpine.

    Alpine uses its native packages; Ubuntu waits for cloud-init to release
    the apt lock and installs Node from NodeSource when needed. Idempotent.
    """
    if not isinstance(major, int) or major < 16:
        raise ValueError(f"Unsupported Node major version: {major}")
    if minimum_version is not None:
        if len(minimum_version) != 3 or any(
            not isinstance(part, int) or part < 0 for part in minimum_version
        ):
            raise ValueError(f"Invalid minimum Node version: {minimum_version!r}")
        if minimum_version[0] != major:
            raise ValueError(
                f"Minimum Node version {minimum_version!r} does not match major {major}"
            )

    required_version = minimum_version or (major, 0, 0)
    required_text = ".".join(str(part) for part in required_version)
    if minimum_version is None:
        requirement_message = f"Node {major}.0.0 or newer is required"
        supported_expression = (
            f"current[0] > {major} || (current[0] === {major} && "
            "(current[1] > minimum[1] || "
            "(current[1] === minimum[1] && current[2] >= minimum[2])))"
        )
    else:
        requirement_message = f"Node {required_text} or newer within the {major}.x line is required"
        supported_expression = (
            f"current[0] === {major} && "
            "(current[1] > minimum[1] || "
            "(current[1] === minimum[1] && current[2] >= minimum[2]))"
        )
    return rf"""
set -euo pipefail
if command -v apk >/dev/null 2>&1; then
    apk add --no-cache bash ca-certificates curl git nodejs npm
else
    command -v cloud-init >/dev/null 2>&1 && cloud-init status --wait >/dev/null 2>&1 || true
    export DEBIAN_FRONTEND=noninteractive

    apt-get update -qq
    apt-get install -y -qq --no-install-recommends curl ca-certificates gnupg git
fi

needs_node=1
if command -v node >/dev/null 2>&1; then
    if node -e '
const current = process.versions.node.split(".").map(Number);
const minimum = [{required_version[0]}, {required_version[1]}, {required_version[2]}];
const supported = {supported_expression};
process.exit(supported ? 0 : 1);
'; then
        needs_node=0
    fi
fi
if [ "$needs_node" = "1" ]; then
    if command -v apk >/dev/null 2>&1; then
        echo "This Alpine image does not provide a compatible Node.js version:" \
            "{requirement_message}. Run '$SMOLVM_NODE_RECOVERY_COMMAND'" \
            "to create an Ubuntu sandbox." >&2
        exit 1
    else
        curl -fsSL https://deb.nodesource.com/setup_{major}.x | bash -
        apt-get install -y -qq --no-install-recommends nodejs
    fi
fi

node -e '
const current = process.versions.node.split(".").map(Number);
const minimum = [{required_version[0]}, {required_version[1]}, {required_version[2]}];
const supported = {supported_expression};
if (!supported) {{
  console.error(
    "{requirement_message}; found " +
      process.versions.node + "."
  );
  process.exit(1);
}}
'
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


def npm_install_global(
    package: str,
    *,
    version: str | None = None,
    allow_scripts: bool = False,
    verify_executable: str | None = None,
) -> str:
    """Return a script that globally installs *package* via npm.

    Assumes Node is already on PATH — pair this with
    :data:`NODE20_BOOTSTRAP` as the preset's ``setup_script`` so that
    system-package/Node phase and the npm phase show up as two separate
    progress steps in the CLI. When ``verify_executable`` is set, the
    installed command must report the pinned package version.
    """
    if not _SAFE_NPM_NAME_RE.match(package):
        raise ValueError(f"Refusing to install unsafe npm package name: {package!r}")
    if version is not None and not _SAFE_NPM_VERSION_RE.match(version):
        raise ValueError(f"Refusing to install unsafe npm package version: {version!r}")
    if verify_executable is not None:
        if not _SAFE_EXECUTABLE_RE.match(verify_executable):
            raise ValueError(f"Refusing to run unsafe executable name: {verify_executable!r}")
        if version is None:
            raise ValueError("An npm package version is required when verifying its executable")
    package_spec = f"{package}@{version}" if version is not None else package
    lifecycle_policy = "npm_lifecycle_arg=\n"
    if allow_scripts:
        lifecycle_policy += rf"""
npm_version=$(npm --version)
npm_major=${{npm_version%%.*}}
npm_rest=${{npm_version#*.}}
npm_minor=${{npm_rest%%.*}}
if [ "${{npm_major:-0}}" -ge 12 ] || {{
    [ "${{npm_major:-0}}" -eq 11 ] && [ "${{npm_minor:-0}}" -ge 16 ];
}}; then
    npm_lifecycle_arg=--allow-scripts={package}
fi
"""
    # Cleaning the cache after install removes ~350-700 MB of leftover
    # tarballs from /root/.npm/_cacache; npm rebuilds it on demand.
    verify_command = (
        f"{verify_executable} --version | grep -F {version}\n" if verify_executable else ""
    )
    return (
        "set -euo pipefail\n"
        f"{lifecycle_policy}"
        f'npm install -g --silent ${{npm_lifecycle_arg:+"$npm_lifecycle_arg"}} {package_spec}\n'
        "npm cache clean --force >/dev/null 2>&1 || true\n"
        f"{verify_command}"
    )


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
