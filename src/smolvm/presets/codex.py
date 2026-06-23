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

from smolvm.presets._scripts import NODE20_BOOTSTRAP, npm_install_global
from smolvm.presets._types import HostConfigCopy, Preset

# Keep ~/.codex transfers to durable auth/config only. Real Codex homes
# contain large logs, sessions, sqlite WALs, caches, plugins, packages,
# attachments, and worktrees that are not needed for login or config.
CODEX_CONFIG_INCLUDE_PATTERNS: tuple[str, ...] = (
    "auth.json",
    "config.toml",
    "*.config.toml",
    "requirements.toml",
    "hooks.json",
    "AGENTS.md",
    "AGENTS.override.md",
    "rules",
    "rules/**",
    "agents",
    "agents/*.toml",
)

CODEX_CONFIG_EXCLUDE_PATTERNS: tuple[str, ...] = (
    ".tmp",
    ".tmp/**",
    "app-server-control",
    "app-server-control/**",
    "app-server-daemon",
    "app-server-daemon/**",
    "archived_sessions",
    "archived_sessions/**",
    "attachments",
    "attachments/**",
    "cache",
    "cache/**",
    "history.jsonl",
    "log",
    "log/**",
    "logs*",
    "memories",
    "memories/**",
    "models",
    "models/**",
    "models_cache.json",
    "packages",
    "packages/**",
    "plugins/.remote-plugin-install-staging",
    "plugins/.remote-plugin-install-staging/**",
    "plugins/cache",
    "plugins/cache/**",
    "session_index.jsonl",
    "shell_snapshots",
    "shell_snapshots/**",
    "skills",
    "skills/**",
    "tmp",
    "tmp/**",
    "worktrees",
    "worktrees/**",
    "*.db",
    "*.db-*",
    "*.sqlite",
    "*.sqlite-*",
)

CODEX_HOST_CONFIGS: tuple[HostConfigCopy, ...] = (
    HostConfigCopy(
        host_path="~/.codex",
        guest_path="/root/.codex",
        include_patterns=CODEX_CONFIG_INCLUDE_PATTERNS,
        exclude_patterns=CODEX_CONFIG_EXCLUDE_PATTERNS,
    ),
)

CODEX_PRESET = Preset(
    name="codex",
    summary="Start a sandbox with OpenAI's codex CLI preinstalled.",
    setup_script=NODE20_BOOTSTRAP,
    install_script=npm_install_global("@openai/codex"),
    host_env_vars=("OPENAI_API_KEY",),
    host_configs=CODEX_HOST_CONFIGS,
    launch_command="codex",
)
