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

"""Pi coding agent preset (https://pi.dev/)."""

from __future__ import annotations

from smolvm.presets._scripts import NODE20_BOOTSTRAP, npm_install_global
from smolvm.presets._types import HostConfigCopy, Preset
from smolvm.presets.claude_code import CLAUDE_CODE_KEYCHAIN_SECRET, CLAUDE_RESET_INSTALL_METHOD

# Pi is a meta-harness: its `/login` flow lists "OpenAI ChatGPT Plus/Pro
# (Codex)" and "Anthropic Claude Pro/Max" as subscription providers, and
# in practice Pi reuses each provider CLI's on-disk credentials when the
# user is already logged in there. So forward the union of codex's and
# claude-code's host-config bundles plus Pi's own ~/.pi (which contains
# settings, sessions, and ~/.pi/agent/auth.json — Pi's own OAuth tokens).
# We also append claude_code's reset snippet so the copied
# ~/.claude.json doesn't carry a host-specific installMethod into the
# guest.
PI_PRESET = Preset(
    name="pi",
    summary="Start a sandbox with the Pi coding agent preinstalled.",
    setup_script=NODE20_BOOTSTRAP,
    install_script=(
        npm_install_global("@mariozechner/pi-coding-agent") + CLAUDE_RESET_INSTALL_METHOD
    ),
    host_env_vars=("ANTHROPIC_API_KEY", "OPENAI_API_KEY"),
    host_configs=(
        HostConfigCopy(host_path="~/.pi", guest_path="/root/.pi"),
        HostConfigCopy(host_path="~/.codex", guest_path="/root/.codex"),
        HostConfigCopy(host_path="~/.claude.json", guest_path="/root/.claude.json"),
        HostConfigCopy(host_path="~/.claude", guest_path="/root/.claude"),
    ),
    host_keychain_secrets=(CLAUDE_CODE_KEYCHAIN_SECRET,),
    launch_command="pi",
)
