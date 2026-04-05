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

"""Tests for SmolVM utils module."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from smolvm.exceptions import SmolVMError
from smolvm.utils import ensure_ssh_key, run_command, which


class TestRunCommand:
    """Tests for run_command utility."""

    @patch("smolvm.utils.subprocess.run")
    def test_run_command_success(self, mock_run: MagicMock) -> None:
        """Test successful command execution."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["echo", "hi"], returncode=0, stdout="hi\n", stderr=""
        )

        result = run_command(["echo", "hi"], use_sudo=False)

        assert result.returncode == 0
        assert result.stdout == "hi\n"
        mock_run.assert_called_once()

    @patch("smolvm.utils.subprocess.run")
    def test_run_command_failure_raises(self, mock_run: MagicMock) -> None:
        """Test that non-zero exit code raises SmolVMError."""
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1, cmd=["false"], stderr="bad"
        )

        with pytest.raises(SmolVMError, match="Command failed"):
            run_command(["false"], use_sudo=False)

    @patch("smolvm.utils.subprocess.run")
    def test_run_command_timeout_raises(self, mock_run: MagicMock) -> None:
        """Test that timeout raises SmolVMError."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["sleep", "99"], timeout=30)

        with pytest.raises(SmolVMError, match="Command timed out"):
            run_command(["sleep", "99"], use_sudo=False)

    @patch("smolvm.utils.os.geteuid", return_value=1000)
    @patch("smolvm.utils.subprocess.run")
    def test_run_command_sudo_when_not_root(
        self, mock_run: MagicMock, mock_geteuid: MagicMock
    ) -> None:
        """Test sudo prefix when not root."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["sudo", "-n", "ls"], returncode=0, stdout="", stderr=""
        )

        run_command(["ls"], use_sudo=True)

        call_args = mock_run.call_args
        assert call_args[0][0][0] == "sudo"
        assert call_args[0][0][1] == "-n"

    @patch("smolvm.utils.os.geteuid", return_value=1000)
    @patch("smolvm.utils.subprocess.run")
    def test_run_command_sudo_auth_failure_has_setup_hint(
        self, mock_run: MagicMock, mock_geteuid: MagicMock
    ) -> None:
        """Test sudo auth failures include one-time setup guidance."""
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd=["sudo", "-n", "ip", "link", "show"],
            stderr="sudo: a password is required",
        )

        with pytest.raises(SmolVMError, match="smolvm setup"):
            run_command(["ip", "link", "show"], use_sudo=True)

    @patch("smolvm.utils.os.geteuid", return_value=0)
    @patch("smolvm.utils.subprocess.run")
    def test_run_command_no_sudo_when_root(
        self, mock_run: MagicMock, mock_geteuid: MagicMock
    ) -> None:
        """Test no sudo prefix when root."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ls"], returncode=0, stdout="", stderr=""
        )

        run_command(["ls"], use_sudo=True)

        call_args = mock_run.call_args
        assert call_args[0][0][0] == "ls"

    def test_run_command_empty_cmd_raises(self) -> None:
        """Test that empty command raises ValueError."""
        with pytest.raises(ValueError, match="cmd cannot be empty"):
            run_command([])

    def test_run_command_none_cmd_raises(self) -> None:
        """Test that None command raises ValueError."""
        with pytest.raises(ValueError, match="cmd cannot be empty"):
            run_command(None)  # type: ignore


class TestWhich:
    """Tests for which utility."""

    @patch("smolvm.utils.shutil.which", return_value="/usr/bin/python3")
    def test_which_found(self, mock_which: MagicMock) -> None:
        """Test finding an existing binary."""
        from pathlib import Path

        result = which("python3")

        assert result == Path("/usr/bin/python3")
        mock_which.assert_called_once_with("python3")

    @patch("smolvm.utils.shutil.which", return_value=None)
    def test_which_not_found(self, mock_which: MagicMock) -> None:
        """Test that missing binary returns None."""
        result = which("nonexistent-binary")

        assert result is None

    def test_which_empty_name_raises(self) -> None:
        """Test that empty binary name raises ValueError."""
        with pytest.raises(ValueError, match="binary name cannot be empty"):
            which("")


class TestEnsureSSHKey:
    """Tests for ensure_ssh_key utility."""

    @patch("smolvm.utils.subprocess.run")
    @patch("smolvm.utils.Path.home")
    def test_default_path_uses_keys_subdir(
        self,
        mock_home: MagicMock,
        mock_run: MagicMock,
        tmp_path,
    ) -> None:
        """Default key location should be ~/.smolvm/keys."""
        mock_home.return_value = tmp_path

        private_key, public_key = ensure_ssh_key()

        expected_dir = tmp_path / ".smolvm" / "keys"
        assert private_key == expected_dir / "id_ed25519"
        assert public_key == expected_dir / "id_ed25519.pub"
        assert expected_dir.exists()
        mock_run.assert_called_once()

    @patch("smolvm.utils.subprocess.run")
    def test_explicit_key_dir_does_not_require_sudo_context(
        self,
        mock_run: MagicMock,
        tmp_path,
    ) -> None:
        """Passing key_dir should work without relying on sudo-derived locals."""
        key_dir = tmp_path / "custom-keys"

        private_key, public_key = ensure_ssh_key(key_dir=key_dir)

        assert private_key == key_dir / "id_ed25519"
        assert public_key == key_dir / "id_ed25519.pub"
        mock_run.assert_called_once()
