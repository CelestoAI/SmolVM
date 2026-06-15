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

import json

from smolvm.presets._scripts import NODE20_BOOTSTRAP, npm_install_global
from smolvm.presets._types import HostConfigCopy, HostKeychainSecret, Preset

# The host ~/.claude.json is ~130 KB, but ~90% of it is data the guest
# must not inherit: `projects` (per-host directory trust decisions and
# prior session history — a privacy leak into a disposable sandbox) and
# `cached*` feature-flag blobs (re-fetched in the guest anyway). The
# interactive CLI only needs a tiny auth/onboarding subset to skip its
# login screen — `claude auth status` returns `loggedIn: true` from just
# these keys, with email/org/plan re-derived from the OAuth token in
# .credentials.json.
#
# An allowlist (not a denylist) is deliberate: claude-code adds new
# top-level keys every release, so a "strip these junk keys" list would
# rot. The load-bearing keys below are stable.
#
# Notably absent: `installMethod`. The host records "native" when the
# user ran `claude migrate-installer`; in the guest claude is
# npm-installed, so a copied "native" tag made claude error with
# "claude command not found at /root/.local/bin/claude". Omitting the
# key from the allowlist lets claude re-detect the install method fresh
# — replacing the old in-guest reset script.
_CLAUDE_JSON_AUTH_KEYS = (
    "oauthAccount",
    "userID",
    "anonymousId",
    "hasCompletedOnboarding",
    "firstStartTime",
    "claudeCodeFirstTokenDate",
)


def minimize_claude_json(raw: bytes) -> bytes:
    """Project the host ~/.claude.json down to the auth/onboarding subset.

    Returns the filtered JSON as bytes ready to write into the guest.
    Falls back to an empty JSON object if the host file is malformed —
    or is valid JSON that isn't an object (``null``, a list, a string) —
    so a corrupt host config can't abort sandbox provisioning; the guest
    still has a valid (if login-prompting) file.

    Public so the Pi preset can reuse it when it forwards ~/.claude.json
    (Pi delegates Claude Pro/Max auth through Claude Code's on-disk
    config).
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        data = {}
    # ~/.claude.json is always an object in practice, but guard the
    # allowlist projection against valid-but-non-object JSON so the key
    # lookups below can't raise and defeat the fallback intent.
    if not isinstance(data, dict):
        data = {}
    minimal = {k: data[k] for k in _CLAUDE_JSON_AUTH_KEYS if k in data}
    return json.dumps(minimal, indent=2).encode("utf-8")


# On macOS, claude stores its OAuth tokens in the keychain (not in
# ~/.claude/.credentials.json — that file does not exist on a
# signed-in Mac). Pull the keychain item into the guest as the
# credentials file Linux claude reads at startup; without this the
# guest greets the user by name (from oauthAccount in the copied
# ~/.claude.json) but says "Not logged in". The keychain item is
# named "Claude Code-credentials" and its value is already the JSON
# body credentials.json expects.
#
# Public so the Pi preset can reuse the same secret — Pi reads
# ~/.claude/.credentials.json when delegating to the Claude
# subscription, so the keychain extraction has to happen there too.
CLAUDE_CODE_KEYCHAIN_SECRET = HostKeychainSecret(
    service="Claude Code-credentials",
    guest_path="/root/.claude/.credentials.json",
)

CLAUDE_CODE_PRESET = Preset(
    name="claude-code",
    aliases=("claude",),
    summary="Start a sandbox with Anthropic's Claude Code CLI preinstalled.",
    setup_script=NODE20_BOOTSTRAP,
    install_script=npm_install_global("@anthropic-ai/claude-code"),
    host_env_vars=("ANTHROPIC_API_KEY",),
    host_configs=(
        # Minimize ~/.claude.json, then copy only the on-disk token file
        # from ~/.claude. Linux stores Claude OAuth there; macOS usually
        # lacks this file and uses the keychain secret below, which runs
        # after config copies and can overwrite stale on-disk credentials.
        # Copying the whole ~/.claude dir would drag host caches/backups
        # into the sandbox for no benefit.
        HostConfigCopy(
            host_path="~/.claude.json",
            guest_path="/root/.claude.json",
            file_mode=0o600,
            transform=minimize_claude_json,
        ),
        HostConfigCopy(
            host_path="~/.claude/.credentials.json",
            guest_path="/root/.claude/.credentials.json",
            file_mode=0o600,
        ),
    ),
    host_keychain_secrets=(CLAUDE_CODE_KEYCHAIN_SECRET,),
    launch_command="claude",
)
