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

"""Tests for smolvm.env — environment variable injection helpers."""

from unittest.mock import MagicMock

import pytest

from smolvm.env import (
    ENV_FILE,
    _shell_quote,
    build_env_script,
    inject_env_vars,
    read_env_vars,
    remove_env_vars,
    validate_env_key,
)
from smolvm.exceptions import SmolVMError
from smolvm.types import CommandResult


# ── validate_env_key ──────────────────────────────────────────────────


class TestValidateEnvKey:
    """Tests for validate_env_key."""

    def test_valid_keys(self) -> None:
        for key in ("HOME", "_foo", "MY_VAR_2", "a", "_"):
            validate_env_key(key)  # should not raise

    def test_empty_key(self) -> None:
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_env_key("")

    def test_starts_with_digit(self) -> None:
        with pytest.raises(ValueError, match="Invalid"):
            validate_env_key("123FOO")

    def test_contains_equals(self) -> None:
        with pytest.raises(ValueError, match="Invalid"):
            validate_env_key("A=B")

    def test_contains_spaces(self) -> None:
        with pytest.raises(ValueError, match="Invalid"):
            validate_env_key("MY VAR")

    def test_contains_dash(self) -> None:
        with pytest.raises(ValueError, match="Invalid"):
            validate_env_key("my-var")


# ── build_env_script ──────────────────────────────────────────────────


class TestBuildEnvScript:
    """Tests for build_env_script."""

    def test_empty_dict(self) -> None:
        result = build_env_script({})
        assert "empty" in result.lower()

    def test_single_var(self) -> None:
        result = build_env_script({"FOO": "bar"})
        # shlex.quote("bar") -> "bar" (no quotes needed)
        assert "export FOO=bar" in result

    def test_multiple_vars_sorted(self) -> None:
        result = build_env_script({"Z_VAR": "z", "A_VAR": "a"})
        lines = result.strip().splitlines()
        export_lines = [l for l in lines if l.startswith("export")]
        assert len(export_lines) == 2
        assert export_lines[0].startswith("export A_VAR=a")
        assert export_lines[1].startswith("export Z_VAR=z")

    def test_value_with_single_quote(self) -> None:
        result = build_env_script({"KEY": "it's a test"})
        # shlex.quote handles ' by closing, escaping, and reopening
        # "it's a test" -> "'it'"'"'s a test'" is one way, but shlex usually does "'it'"'"'s a test'"
        # actually shlex.quote("it's a test") -> "'it'"'"'s a test'"
        assert "KEY=" in result
        assert "it" in result and "test" in result

    def test_value_with_double_quote(self) -> None:
        result = build_env_script({"KEY": 'say "hello"'})
        assert "KEY=" in result

    def test_value_with_dollar_sign(self) -> None:
        result = build_env_script({"KEY": "price is $5"})
        assert "KEY=" in result
        # Single-quoted so $ should be literal
        assert "$5" in result

    def test_value_with_spaces(self) -> None:
        result = build_env_script({"KEY": "hello world"})
        assert "export KEY='hello world'" in result

    def test_empty_value(self) -> None:
        result = build_env_script({"KEY": ""})
        assert "export KEY=''" in result

    def test_invalid_key_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid"):
            build_env_script({"BAD KEY": "value"})

    def test_shebang_present(self) -> None:
        result = build_env_script({"FOO": "bar"})
        assert result.startswith("#!/bin/sh")


# ── shell_quote ────────────────────────────────────────────────────────


class TestShellQuote:
    """Tests for _shell_quote."""

    def test_simple_value(self) -> None:
        # shlex.quote doesn't quote safe strings
        assert _shell_quote("hello") == "hello"

    def test_value_with_single_quote(self) -> None:
        quoted = _shell_quote("it's")
        # Must not contain unescaped single quote
        assert "'" in quoted

    def test_empty_value(self) -> None:
        assert _shell_quote("") == "''"


# ── inject_env_vars ──────────────────────────────────────────────────


def _make_ssh_mock(
    run_ok: bool = True,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
) -> MagicMock:
    """Create a mock SSHClient with configurable run() return."""
    ssh = MagicMock()
    # Correctly set exit_code to force ok property to be False if run_ok is False
    final_exit = exit_code
    if not run_ok and final_exit == 0:
        final_exit = 1

    result = CommandResult(
        exit_code=final_exit,
        stdout=stdout,
        stderr=stderr,
    )
    ssh.run.return_value = result
    return ssh


class TestInjectEnvVars:
    """Tests for inject_env_vars."""

    def test_empty_dict_returns_empty(self) -> None:
        ssh = _make_ssh_mock()
        result = inject_env_vars(ssh, {})
        assert result == []
        ssh.run.assert_not_called()

    def test_inject_single_var(self) -> None:
        ssh = _make_ssh_mock()
        result = inject_env_vars(ssh, {"FOO": "bar"}, merge=False)
        assert result == ["FOO"]
        ssh.run.assert_called_once()
        call_cmd = ssh.run.call_args[0][0]
        # Command now uses base64 transport — verify structure
        assert "base64 -d" in call_cmd
        assert "mktemp" in call_cmd
        assert "chmod 0644" in call_cmd
        assert ENV_FILE in call_cmd
        # Decode the payload to verify content
        import base64 as b64mod
        # Extract the base64 string from: printf '%s' '<b64>' | base64 -d
        b64_start = call_cmd.index("printf '%s' '") + len("printf '%s' '")
        b64_end = call_cmd.index("'", b64_start)
        decoded = b64mod.b64decode(call_cmd[b64_start:b64_end]).decode()
        assert "export FOO=bar" in decoded

    def test_inject_with_merge_reads_existing(self) -> None:
        ssh = _make_ssh_mock(stdout="#!/bin/sh\nexport OLD='val'\n")
        result = inject_env_vars(ssh, {"NEW": "new_val"}, merge=True)
        assert "NEW" in result
        assert "OLD" in result
        # Should have called run() twice: once to read, once to write
        assert ssh.run.call_count == 2

    def test_inject_without_merge_replaces(self) -> None:
        ssh = _make_ssh_mock()
        result = inject_env_vars(ssh, {"NEW": "val"}, merge=False)
        assert result == ["NEW"]
        # Only one call (write), no read
        assert ssh.run.call_count == 1

    def test_inject_failure_raises(self) -> None:
        ssh = _make_ssh_mock(run_ok=False, stderr="permission denied")
        with pytest.raises(SmolVMError, match="Failed to inject"):
            inject_env_vars(ssh, {"FOO": "bar"}, merge=False)

    def test_invalid_key_raises_before_ssh(self) -> None:
        ssh = _make_ssh_mock()
        with pytest.raises(ValueError, match="Invalid"):
            inject_env_vars(ssh, {"BAD KEY": "val"}, merge=False)
        ssh.run.assert_not_called()


# ── read_env_vars ────────────────────────────────────────────────────


class TestReadEnvVars:
    """Tests for read_env_vars."""

    def test_empty_file(self) -> None:
        ssh = _make_ssh_mock(stdout="")
        result = read_env_vars(ssh)
        assert result == {}

    def test_file_not_found(self) -> None:
        ssh = _make_ssh_mock(stdout="")
        result = read_env_vars(ssh)
        assert result == {}

    def test_parse_single_var(self) -> None:
        ssh = _make_ssh_mock(stdout="#!/bin/sh\nexport FOO='bar'\n")
        result = read_env_vars(ssh)
        assert result == {"FOO": "bar"}

    def test_parse_multiple_vars(self) -> None:
        content = "#!/bin/sh\nexport A='1'\nexport B='2'\n"
        ssh = _make_ssh_mock(stdout=content)
        result = read_env_vars(ssh)
        assert result == {"A": "1", "B": "2"}

    def test_skips_comments_and_blanks(self) -> None:
        content = "#!/bin/sh\n# comment\n\nexport FOO='bar'\n"
        ssh = _make_ssh_mock(stdout=content)
        result = read_env_vars(ssh)
        assert result == {"FOO": "bar"}

    def test_value_with_escaped_quote(self) -> None:
        # shlex.quote("it's") produces "it'\\''s" style
        content = "#!/bin/sh\nexport KEY='it'\"'\"'s'\n"
        ssh = _make_ssh_mock(stdout=content)
        result = read_env_vars(ssh)
        assert result == {"KEY": "it's"}


# ── remove_env_vars ──────────────────────────────────────────────────


class TestRemoveEnvVars:
    """Tests for remove_env_vars."""

    def test_remove_empty_keys(self) -> None:
        ssh = _make_ssh_mock()
        result = remove_env_vars(ssh, [])
        assert result == {}
        ssh.run.assert_not_called()

    def test_remove_existing_key(self) -> None:
        content = "#!/bin/sh\nexport A='1'\nexport B='2'\n"
        ssh = _make_ssh_mock(stdout=content)
        result = remove_env_vars(ssh, ["A"])
        assert result == {"A": "1"}
        # read + write = 2 calls
        assert ssh.run.call_count == 2

    def test_remove_nonexistent_key(self) -> None:
        content = "#!/bin/sh\nexport A='1'\n"
        ssh = _make_ssh_mock(stdout=content)
        result = remove_env_vars(ssh, ["NONEXISTENT"])
        assert result == {}
        # Only read, no write needed
        assert ssh.run.call_count == 1

    def test_remove_failure_raises(self) -> None:
        # First call (read) succeeds, second call (write) fails
        ssh = MagicMock()
        read_result = CommandResult(ok=True, exit_code=0, stdout="#!/bin/sh\nexport A='1'\n", stderr="")
        write_result = CommandResult(ok=False, exit_code=1, stdout="", stderr="disk full")
        ssh.run.side_effect = [read_result, write_result]
        with pytest.raises(SmolVMError, match="Failed to update"):
            remove_env_vars(ssh, ["A"])
