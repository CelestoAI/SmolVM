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

"""Anthropic Claude Code CLI preset."""

from __future__ import annotations

from smolvm.presets._scripts import npm_install_global
from smolvm.presets._types import HostConfigCopy, Preset

CLAUDE_CODE_PRESET = Preset(
    name="claude-code",
    aliases=("claude",),
    summary="Start a sandbox with Anthropic's Claude Code CLI preinstalled.",
    install_script=npm_install_global("@anthropic-ai/claude-code"),
    host_env_vars=("ANTHROPIC_API_KEY",),
    host_configs=(
        HostConfigCopy(host_path="~/.claude.json", guest_path="/root/.claude.json"),
        HostConfigCopy(host_path="~/.claude", guest_path="/root/.claude"),
    ),
    launch_command="claude",
)
