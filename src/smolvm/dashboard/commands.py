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

"""Command-style parser for the Nebula command bar.

Parses deterministic commands into SmolVMManager actions.
Natural-language parsing is intentionally deferred.
"""

from __future__ import annotations

import logging
import shlex
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


_VERB_ALIASES: dict[str, CommandAction] = {
    "list": CommandAction.LIST,
    "ls": CommandAction.LIST,
    "show": CommandAction.LIST,
    "delete": CommandAction.DELETE,
    "kill": CommandAction.DELETE,
    "remove": CommandAction.DELETE,
    "stop": CommandAction.STOP,
    "info": CommandAction.INFO,
    "inspect": CommandAction.INFO,
    "detail": CommandAction.INFO,
    "details": CommandAction.INFO,
}


def _unknown(raw_input: str, target: str = "") -> ParsedCommand:
    return ParsedCommand(
        action=CommandAction.UNKNOWN,
        target=target,
        raw_input=raw_input,
        params={},
    )


def parse_command(raw_input: str) -> ParsedCommand:
    """Parse a command-style input from the command bar.

    Args:
        raw_input: Raw text from the user.

    Returns:
        ParsedCommand with action, target, and metadata.

    Examples:
        >>> parse_command("list running")
        ParsedCommand(action=LIST, target="running", ...)

        >>> parse_command("delete vm-abc123")
        ParsedCommand(action=DELETE, target="vm-abc123", ...)

        >>> parse_command("info vm-abc123")
        ParsedCommand(action=INFO, target="vm-abc123", ...)
    """
    text = raw_input.strip()
    if not text:
        return _unknown(raw_input)

    try:
        tokens = shlex.split(text)
    except ValueError:
        logger.warning("Invalid command quoting: %s", raw_input)
        return _unknown(raw_input, target=text)

    if not tokens:
        return _unknown(raw_input)

    action = _VERB_ALIASES.get(tokens[0].lower())
    if action is None:
        logger.warning("Unknown command verb: %s", tokens[0])
        return _unknown(raw_input, target=text)

    # list [status]
    if action == CommandAction.LIST:
        if len(tokens) == 1:
            target = ""
            params: dict[str, Any] = {}
        elif len(tokens) == 2:
            token = tokens[1].lower()
            target = "" if token in {"all", "vm", "vms"} else token
            params = {"filter": target}
        else:
            return _unknown(raw_input, target=text)

        logger.info("Parsed command: action=%s target='%s'", action.value, target)
        return ParsedCommand(
            action=action,
            target=target,
            raw_input=raw_input,
            params=params,
        )

    # info/delete/stop require exactly one target token.
    if len(tokens) != 2:
        return _unknown(raw_input, target=text)

    target = tokens[1]
    logger.info("Parsed command: action=%s target='%s'", action.value, target)
    return ParsedCommand(
        action=action,
        target=target,
        raw_input=raw_input,
        params={"target": target},
    )
