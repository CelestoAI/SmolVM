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

"""NousResearch Hermes agent preset (https://hermes-agent.nousresearch.com/)."""

from __future__ import annotations

from smolvm.presets._scripts import PYTHON_BOOTSTRAP
from smolvm.presets._types import HostConfigCopy, Preset

_HERMES_REPO = "https://github.com/NousResearch/hermes-agent.git"
_HERMES_INSTALL_DIR = "/opt/hermes-agent"

HERMES_INSTALL_SCRIPT = rf"""
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

if [ ! -d {_HERMES_INSTALL_DIR} ]; then
    git clone --depth 1 {_HERMES_REPO} {_HERMES_INSTALL_DIR}
else
    git -C {_HERMES_INSTALL_DIR} pull --ff-only || true
fi

cd {_HERMES_INSTALL_DIR}
uv venv
uv pip install -e ".[all]" || uv pip install -e .
ln -sf {_HERMES_INSTALL_DIR}/.venv/bin/hermes /usr/local/bin/hermes
"""

HERMES_PRESET = Preset(
    name="hermes",
    summary="Start a sandbox with the Hermes agent preinstalled.",
    setup_script=PYTHON_BOOTSTRAP,
    install_script=HERMES_INSTALL_SCRIPT,
    host_env_vars=("OPENROUTER_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "HF_TOKEN"),
    host_configs=(HostConfigCopy(host_path="~/.hermes", guest_path="/root/.hermes"),),
    default_disk_mib=10240,
    launch_command="hermes",
    no_env_hint=(
        "No API key found. Set OPENROUTER_API_KEY on your machine,"
        " or run 'hermes setup' inside the sandbox."
    ),
)
