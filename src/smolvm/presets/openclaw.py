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

"""OpenClaw CLI preset (https://github.com/openclaw/openclaw)."""

from __future__ import annotations

from smolvm.presets._scripts import node_bootstrap, npm_install_global
from smolvm.presets._types import HostConfigCopy, Preset

OPENCLAW_PRESET = Preset(
    name="openclaw",
    aliases=("claw",),
    summary="Start a sandbox with the OpenClaw CLI preinstalled.",
    setup_script=node_bootstrap(22),
    install_script=npm_install_global("openclaw"),
    host_env_vars=("OPENROUTER_API_KEY", "OPENAI_API_KEY"),
    host_configs=(HostConfigCopy(host_path="~/.openclaw", guest_path="/root/.openclaw"),),
    launch_command="openclaw",
    no_env_hint=(
        "No API key found. Set OPENROUTER_API_KEY or OPENAI_API_KEY on your"
        " machine, or run 'openclaw onboard' inside the sandbox."
    ),
)
