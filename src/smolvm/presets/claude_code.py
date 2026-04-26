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

from smolvm.presets._scripts import NODE20_BOOTSTRAP, npm_install_global
from smolvm.presets._types import HostConfigCopy, HostKeychainSecret, Preset

# Drop host-specific install metadata from the copied ~/.claude.json.
# claude-code records `installMethod` ("native" if the user ran
# `claude migrate-installer` on their host) and uses it to locate its
# own binary on launch. That value reflects the host's layout, not the
# guest's — in the guest claude is npm-installed under
# /usr/lib/node_modules, so a copied "native" tag makes claude error
# with "claude command not found at /root/.local/bin/claude" on first
# start. Removing the key lets claude re-detect the install method
# fresh on the guest. python3 ships in the Ubuntu cloud image.
_RESET_INSTALL_METHOD = r"""
if [ -f /root/.claude.json ]; then
  python3 - <<'PY' || true
import json, pathlib
p = pathlib.Path("/root/.claude.json")
try:
    data = json.loads(p.read_text())
except Exception:
    raise SystemExit(0)
if isinstance(data, dict) and "installMethod" in data:
    del data["installMethod"]
    p.write_text(json.dumps(data, indent=2))
PY
fi
"""

CLAUDE_CODE_PRESET = Preset(
    name="claude-code",
    aliases=("claude",),
    summary="Start a sandbox with Anthropic's Claude Code CLI preinstalled.",
    setup_script=NODE20_BOOTSTRAP,
    install_script=npm_install_global("@anthropic-ai/claude-code") + _RESET_INSTALL_METHOD,
    host_env_vars=("ANTHROPIC_API_KEY",),
    host_configs=(
        HostConfigCopy(host_path="~/.claude.json", guest_path="/root/.claude.json"),
        HostConfigCopy(host_path="~/.claude", guest_path="/root/.claude"),
    ),
    # On macOS, claude stores its OAuth tokens in the keychain (not in
    # ~/.claude/.credentials.json — that file does not exist on a
    # signed-in Mac). Pull the keychain item into the guest as the
    # credentials file Linux claude reads at startup; without this the
    # guest greets the user by name (from oauthAccount in the copied
    # ~/.claude.json) but says "Not logged in". The keychain item is
    # named "Claude Code-credentials" and its value is already the JSON
    # body credentials.json expects.
    host_keychain_secrets=(
        HostKeychainSecret(
            service="Claude Code-credentials",
            guest_path="/root/.claude/.credentials.json",
        ),
    ),
    launch_command="claude",
)
