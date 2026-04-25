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

"""OpenAI codex CLI preset."""

from __future__ import annotations

from smolvm.presets._scripts import npm_install_global
from smolvm.presets._types import HostConfigCopy, Preset

CODEX_PRESET = Preset(
    name="codex",
    summary="Start a sandbox with OpenAI's codex CLI preinstalled.",
    install_script=npm_install_global("@openai/codex"),
    host_env_vars=("OPENAI_API_KEY",),
    host_configs=(HostConfigCopy(host_path="~/.codex", guest_path="/root/.codex"),),
    launch_command="codex",
)
