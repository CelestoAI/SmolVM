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

"""Preconfigured agent-harness blueprints for ``smolvm <harness> start``.

A *preset* boots a fresh sandbox and layers on a specific agent
harness — for example OpenAI's codex CLI or Anthropic's Claude Code —
along with any host config files and API keys the harness expects.
"""

from __future__ import annotations

from smolvm.presets._git import GIT_HOST_CONFIGS
from smolvm.presets._install import apply_preset, collect_host_env, transfer_keychain_secrets
from smolvm.presets._types import HostConfigCopy, HostKeychainSecret, Preset
from smolvm.presets.claude_code import CLAUDE_CODE_PRESET
from smolvm.presets.codex import CODEX_PRESET
from smolvm.presets.hermes import HERMES_PRESET
from smolvm.presets.openclaw import OPENCLAW_PRESET
from smolvm.presets.pi import PI_PRESET

_BUILTIN_PRESETS: tuple[Preset, ...] = (
    CODEX_PRESET,
    CLAUDE_CODE_PRESET,
    HERMES_PRESET,
    OPENCLAW_PRESET,
    PI_PRESET,
)

_REGISTRY: dict[str, Preset] = {p.name: p for p in _BUILTIN_PRESETS}


def list_presets() -> list[Preset]:
    """Return all registered presets, sorted by name."""
    return sorted(_REGISTRY.values(), key=lambda p: p.name)


def get_preset(name: str) -> Preset:
    """Return the preset with the given name.

    Raises:
        KeyError: If no preset with that name is registered. The message
            lists the available names so callers can surface a helpful
            error to users.
    """
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        available = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"Unknown preset {name!r}. Available: {available}") from exc


def preset_names() -> list[str]:
    """Return registered preset names, sorted."""
    return sorted(_REGISTRY)


def preset_command_names() -> list[str]:
    """Return every name (canonical + aliases) that the CLI accepts.

    Use this — not :func:`preset_names` — when checking whether an
    ``args.command`` came from a preset, since the user may have typed
    an alias like ``claude`` instead of ``claude-code``.
    """
    names: set[str] = set(_REGISTRY)
    for preset in _REGISTRY.values():
        names.update(preset.aliases)
    return sorted(names)


__all__ = [
    "CLAUDE_CODE_PRESET",
    "CODEX_PRESET",
    "GIT_HOST_CONFIGS",
    "HERMES_PRESET",
    "OPENCLAW_PRESET",
    "PI_PRESET",
    "HostConfigCopy",
    "HostKeychainSecret",
    "Preset",
    "apply_preset",
    "collect_host_env",
    "get_preset",
    "transfer_keychain_secrets",
    "list_presets",
    "preset_command_names",
    "preset_names",
]
