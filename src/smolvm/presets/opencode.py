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

"""Stable OpenCode coding agent preset."""

from __future__ import annotations

from smolvm.presets._scripts import NODE20_BOOTSTRAP, npm_install_global
from smolvm.presets._types import HostConfigCopy, Preset

OPENCODE_PRESET = Preset(
    name="opencode",
    summary="Start a sandbox with the stable OpenCode agent preinstalled.",
    setup_script=NODE20_BOOTSTRAP,
    install_script=npm_install_global("opencode-ai"),
    host_env_vars=(
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION",
    ),
    host_configs=(
        HostConfigCopy(
            host_path="~/.config/opencode",
            guest_path="/root/.config/opencode",
        ),
        HostConfigCopy(
            host_path="~/.local/share/opencode/auth.json",
            guest_path="/root/.local/share/opencode/auth.json",
            file_mode=0o600,
        ),
    ),
    launch_command="opencode",
    no_env_hint=(
        "No OpenCode provider credentials were found. Set a provider API key on your machine,"
        " or run 'opencode auth login' inside the sandbox."
    ),
)
