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

"""Tests for dashboard command parsing."""

import pytest

from smolvm.dashboard.commands import CommandAction, parse_command


@pytest.mark.parametrize(
    ("raw", "action", "target"),
    [
        ("list", CommandAction.LIST, ""),
        ("list running", CommandAction.LIST, "running"),
        ("list all", CommandAction.LIST, ""),
        ("LS STOPPED", CommandAction.LIST, "stopped"),
        ("show vms", CommandAction.LIST, ""),
        ("delete vm-abc123", CommandAction.DELETE, "vm-abc123"),
        ("kill vm-abc123", CommandAction.DELETE, "vm-abc123"),
        ('delete "vm alpha"', CommandAction.DELETE, "vm alpha"),
        ("stop all", CommandAction.STOP, "all"),
        ("inspect vm-abc123", CommandAction.INFO, "vm-abc123"),
    ],
)
def test_parse_command_valid_inputs(raw: str, action: CommandAction, target: str) -> None:
    """Parser should recognize supported command-style inputs."""
    parsed = parse_command(raw)

    assert parsed.action == action
    assert parsed.target == target
    assert parsed.raw_input == raw


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "info",
        "list running extra",
        "delete",
        "stop vm-1 now",
        "nonsense command",
        'delete "unterminated',
    ],
)
def test_parse_command_invalid_inputs(raw: str) -> None:
    """Invalid or malformed inputs should produce UNKNOWN."""
    parsed = parse_command(raw)

    assert parsed.action == CommandAction.UNKNOWN
