# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for the Windows env-var injector."""

from unittest.mock import MagicMock

import pytest

from smolvm.env_windows import (
    _MANAGED_KEYS_SENTINEL,
    _ps_single_quote,
    inject_env_vars,
    read_env_vars,
    remove_env_vars,
)
from smolvm.exceptions import SmolVMError


def _ok(stdout: str = "") -> MagicMock:
    """A CommandResult-shaped mock with exit_code=0."""
    return MagicMock(exit_code=0, stdout=stdout, stderr="")


def _fail(stderr: str = "boom") -> MagicMock:
    return MagicMock(exit_code=1, stdout="", stderr=stderr)


# ───────────────────────── helper unit tests ────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("simple", "simple"),
        ("with 'quote'", "with ''quote''"),
        ("multi\nline", "multi\nline"),
        ("$env:USERNAME", "$env:USERNAME"),
        ("back`tick", "back`tick"),
        ("", ""),
    ],
)
def test_ps_single_quote(raw: str, expected: str) -> None:
    """Only single quotes get escaped (doubled). Everything else is literal."""
    assert _ps_single_quote(raw) == expected


# ───────────────────────── inject_env_vars ───────────────────────────────


def test_inject_empty_is_noop() -> None:
    ssh = MagicMock()
    assert inject_env_vars(ssh, {}) == []
    ssh.run.assert_not_called()


def test_inject_validates_keys_before_touching_guest() -> None:
    ssh = MagicMock()
    with pytest.raises(ValueError, match="Invalid environment variable key"):
        inject_env_vars(ssh, {"bad key": "val"})
    ssh.run.assert_not_called()


def test_inject_merge_true_first_time(tmp_path) -> None:  # noqa: ARG001
    """Merge with no existing managed keys: just sets new vars + sentinel."""
    ssh = MagicMock()
    # First _run_ps: read sentinel → empty.
    # Second _run_ps: set the two vars.
    # Third _run_ps: write sentinel.
    ssh.run.side_effect = [_ok(""), _ok(""), _ok("")]

    keys = inject_env_vars(ssh, {"FOO": "bar", "BAZ": "qu'ux"}, merge=True)
    assert keys == ["BAZ", "FOO"]
    assert ssh.run.call_count == 3

    # Set script contains both SetEnvironmentVariable calls with quote escapes.
    set_cmd = ssh.run.call_args_list[1].args[0]
    assert (
        "[Environment]::SetEnvironmentVariable('FOO', 'bar', 'User')"
        in set_cmd
    )
    assert (
        "[Environment]::SetEnvironmentVariable('BAZ', 'qu''ux', 'User')"
        in set_cmd
    )

    # Sentinel update contains both keys, sorted, comma-joined.
    sentinel_cmd = ssh.run.call_args_list[2].args[0]
    assert "BAZ,FOO" in sentinel_cmd
    assert _MANAGED_KEYS_SENTINEL in sentinel_cmd


def test_inject_merge_true_with_existing_keys() -> None:
    """Merge preserves prior managed keys (sentinel grows)."""
    ssh = MagicMock()
    # First: read sentinel → "OLD".
    # Second: set new vars.
    # Third: write updated sentinel.
    ssh.run.side_effect = [_ok("OLD\n"), _ok(""), _ok("")]

    keys = inject_env_vars(ssh, {"NEW": "v"}, merge=True)
    assert keys == ["NEW", "OLD"]
    sentinel_cmd = ssh.run.call_args_list[2].args[0]
    assert "NEW,OLD" in sentinel_cmd


def test_inject_merge_false_clears_previously_managed_keys() -> None:
    """merge=False clears managed keys that aren't in the new set."""
    ssh = MagicMock()
    # 1: read sentinel → "OLD1,OLD2,KEEP".
    # 2: clear OLD1+OLD2 (KEEP stays because it's in the new set).
    # 3: set KEEP+NEW.
    # 4: write new sentinel.
    ssh.run.side_effect = [_ok("OLD1,OLD2,KEEP\n"), _ok(""), _ok(""), _ok("")]

    keys = inject_env_vars(ssh, {"KEEP": "k", "NEW": "n"}, merge=False)
    assert keys == ["KEEP", "NEW"]

    clear_cmd = ssh.run.call_args_list[1].args[0]
    assert "'OLD1', $null, 'User'" in clear_cmd
    assert "'OLD2', $null, 'User'" in clear_cmd
    # KEEP must not be cleared.
    assert "'KEEP', $null" not in clear_cmd

    sentinel_cmd = ssh.run.call_args_list[3].args[0]
    assert "KEEP,NEW" in sentinel_cmd


def test_inject_raises_smolvmerror_on_powershell_failure() -> None:
    ssh = MagicMock()
    ssh.run.side_effect = [_ok(""), _fail("set failed")]
    with pytest.raises(SmolVMError, match="PowerShell env-var command failed"):
        inject_env_vars(ssh, {"FOO": "bar"})


# ───────────────────────── read_env_vars ─────────────────────────────────


def test_read_empty_when_no_sentinel() -> None:
    ssh = MagicMock()
    ssh.run.return_value = _ok("")  # sentinel read returns nothing
    assert read_env_vars(ssh) == {}


def test_read_parses_json_payload() -> None:
    ssh = MagicMock()
    ssh.run.side_effect = [
        _ok("FOO,BAZ\n"),  # sentinel
        # Value lookup returns ConvertTo-Json -Compress output. A value
        # containing '=' must round-trip intact — the old line-split parser
        # used to be the contract here, the JSON parser handles it natively.
        _ok('{"FOO":"bar","BAZ":"qu=ux"}\r\n'),
    ]
    out = read_env_vars(ssh)
    assert out == {"FOO": "bar", "BAZ": "qu=ux"}


def test_read_preserves_multiline_values() -> None:
    """Multi-line env-var values must survive the round trip (was truncated)."""
    ssh = MagicMock()
    multiline = "line1\nline2\nline3"
    ssh.run.side_effect = [
        _ok("CERT\n"),
        _ok('{"CERT":"line1\\nline2\\nline3"}\r\n'),
    ]
    out = read_env_vars(ssh)
    assert out == {"CERT": multiline}


def test_read_raises_on_malformed_json() -> None:
    ssh = MagicMock()
    ssh.run.side_effect = [
        _ok("FOO\n"),
        _ok("not-json-at-all"),
    ]
    with pytest.raises(SmolVMError, match="Failed to parse JSON env-var payload"):
        read_env_vars(ssh)


# ───────────────────────── remove_env_vars ───────────────────────────────


def test_remove_empty_keys_is_noop() -> None:
    ssh = MagicMock()
    assert remove_env_vars(ssh, []) == {}
    ssh.run.assert_not_called()


def test_remove_only_clears_keys_actually_managed() -> None:
    ssh = MagicMock()
    # read_env_vars sequence (sentinel + value lookup).
    ssh.run.side_effect = [
        _ok("FOO,BAZ\n"),                  # initial sentinel read in read_env_vars
        _ok('{"FOO":"bar","BAZ":"q"}\n'),  # value lookup (JSON)
        _ok(""),                           # clear PS run
        _ok("FOO,BAZ\n"),                  # post-clear sentinel read
        _ok(""),                           # sentinel rewrite
    ]
    removed = remove_env_vars(ssh, ["FOO", "MISSING"])
    # Only FOO was actually managed — MISSING is silently ignored.
    assert removed == {"FOO": "bar"}

    clear_cmd = ssh.run.call_args_list[2].args[0]
    assert "'FOO', $null, 'User'" in clear_cmd
    assert "MISSING" not in clear_cmd


# ───────────────────── reserved sentinel guard ────────────────────────────


def test_inject_rejects_reserved_sentinel_name() -> None:
    """Setting SMOLVM_ENV_MANAGED_KEYS via the public API must be rejected."""
    ssh = MagicMock()
    with pytest.raises(ValueError, match="reserved"):
        inject_env_vars(ssh, {_MANAGED_KEYS_SENTINEL: "anything"})
    ssh.run.assert_not_called()


def test_remove_rejects_reserved_sentinel_name() -> None:
    """Removing SMOLVM_ENV_MANAGED_KEYS via the public API must be rejected."""
    ssh = MagicMock()
    with pytest.raises(ValueError, match="reserved"):
        remove_env_vars(ssh, [_MANAGED_KEYS_SENTINEL])
    ssh.run.assert_not_called()
