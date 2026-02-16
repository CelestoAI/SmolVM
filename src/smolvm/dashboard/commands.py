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

"""Natural-language command parser for the Nebula command bar.

Translates simple text commands into SmolVMManager actions.
Phase 1 uses regex matching; future phases may incorporate LLMs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class CommandAction(str, Enum):
    """Supported command bar actions."""

    LIST = "list"
    DELETE = "delete"
    STOP = "stop"
    INFO = "info"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ParsedCommand:
    """Result of parsing a command bar input.

    Attributes:
        action: The resolved action.
        target: Target VM ID or filter (e.g., "all", "error", specific ID).
        raw_input: Original user input.
        params: Additional parsed parameters.
    """

    action: CommandAction
    target: str
    raw_input: str
    params: dict[str, Any]


# --- Regex patterns for command matching ---
_PATTERNS: list[tuple[re.Pattern[str], CommandAction, str]] = [
    # "kill all error", "delete stalled", "kill vm-abc123"
    (
        re.compile(r"^(?:kill|delete|remove)\s+(?:all\s+)?(.+)$", re.IGNORECASE),
        CommandAction.DELETE,
        "target",
    ),
    # "stop all", "stop vm-abc123"
    (
        re.compile(r"^stop\s+(.+)$", re.IGNORECASE),
        CommandAction.STOP,
        "target",
    ),
    # "list", "list running", "show vms"
    (
        re.compile(r"^(?:list|show|ls)\s*(.*)$", re.IGNORECASE),
        CommandAction.LIST,
        "filter",
    ),
    # "info vm-abc123", "inspect vm-abc123"
    (
        re.compile(r"^(?:info|inspect|details?)\s+(.+)$", re.IGNORECASE),
        CommandAction.INFO,
        "target",
    ),
]


def parse_command(raw_input: str) -> ParsedCommand:
    """Parse a natural-language command from the command bar.

    Args:
        raw_input: Raw text from the user.

    Returns:
        ParsedCommand with action, target, and metadata.

    Examples:
        >>> parse_command("kill all error")
        ParsedCommand(action=DELETE, target="error", ...)

        >>> parse_command("list running")
        ParsedCommand(action=LIST, target="running", ...)

        >>> parse_command("info vm-abc123")
        ParsedCommand(action=INFO, target="vm-abc123", ...)
    """
    text = raw_input.strip()
    if not text:
        return ParsedCommand(
            action=CommandAction.UNKNOWN,
            target="",
            raw_input=raw_input,
            params={},
        )

    for pattern, action, param_name in _PATTERNS:
        match = pattern.match(text)
        if match:
            value = match.group(1).strip() if match.group(1) else ""
            logger.info(
                "Parsed command: action=%s target='%s'",
                action.value,
                value,
            )
            return ParsedCommand(
                action=action,
                target=value,
                raw_input=raw_input,
                params={param_name: value},
            )

    logger.warning("Unrecognized command: %s", raw_input)
    return ParsedCommand(
        action=CommandAction.UNKNOWN,
        target=text,
        raw_input=raw_input,
        params={},
    )
