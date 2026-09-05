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

"""Tests for smolvm update."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from smolvm.cli.update import (
    _check_for_stable_update,
    _is_uv_tool_install,
    run_update,
)


class TestCheckForStableUpdate:
    def test_returns_none_when_already_latest(self) -> None:
        with (
            patch("smolvm.cli.update._get_current_version", return_value="1.0.0"),
            patch("smolvm.cli.update._fetch_latest_from_pypi", return_value="1.0.0"),
        ):
            current, latest = _check_for_stable_update()
            assert current == "1.0.0"
            assert latest is None

    def test_returns_latest_when_update_available(self) -> None:
        with (
            patch("smolvm.cli.update._get_current_version", return_value="0.9.0"),
            patch("smolvm.cli.update._fetch_latest_from_pypi", return_value="1.0.0"),
        ):
            current, latest = _check_for_stable_update()
            assert current == "0.9.0"
            assert latest == "1.0.0"

    def test_returns_none_on_network_failure(self) -> None:
        with (
            patch("smolvm.cli.update._get_current_version", return_value="1.0.0"),
            patch("smolvm.cli.update._fetch_latest_from_pypi", return_value=None),
        ):
            current, latest = _check_for_stable_update()
            assert current == "1.0.0"
            assert latest is None

    def test_handles_missing_current_version(self) -> None:
        with (
            patch("smolvm.cli.update._get_current_version", return_value=None),
            patch("smolvm.cli.update._fetch_latest_from_pypi", return_value="1.0.0"),
        ):
            current, latest = _check_for_stable_update()
            assert current is None
            assert latest is None


class TestIsUvToolInstall:
    def test_returns_false_when_uv_not_found(self) -> None:
        with patch("smolvm.cli.update.shutil.which", return_value=None):
            assert _is_uv_tool_install() is False

    def test_returns_true_when_smolvm_in_uv_tool_list(self) -> None:
        mock_result = MagicMock()
        mock_result.stdout = "smolvm v0.0.19\n"
        with (
            patch("smolvm.cli.update.shutil.which", return_value="/usr/bin/uv"),
            patch("smolvm.cli.update.subprocess.run", return_value=mock_result),
        ):
            assert _is_uv_tool_install() is True

    def test_returns_false_when_only_smolvm_core_in_uv_tool_list(self) -> None:
        mock_result = MagicMock()
        mock_result.stdout = "smolvm-core v0.0.14\n"
        with (
            patch("smolvm.cli.update.shutil.which", return_value="/usr/bin/uv"),
            patch("smolvm.cli.update.subprocess.run", return_value=mock_result),
        ):
            assert _is_uv_tool_install() is False

    def test_returns_false_when_smolvm_not_in_uv_tool_list(self) -> None:
        mock_result = MagicMock()
        mock_result.stdout = "other-tool v1.0\n"
        with (
            patch("smolvm.cli.update.shutil.which", return_value="/usr/bin/uv"),
            patch("smolvm.cli.update.subprocess.run", return_value=mock_result),
        ):
            assert _is_uv_tool_install() is False


class TestRunUpdate:
    def test_check_only_no_update(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("smolvm.cli.update._check_for_stable_update", return_value=("1.0.0", None)),
        ):
            rc = run_update(check=True)
        assert rc == 0
        out = capsys.readouterr().out
        assert "up to date" in out

    def test_check_only_unknown_version(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("smolvm.cli.update._check_for_stable_update", return_value=(None, None)),
        ):
            rc = run_update(check=True)
        assert rc == 1
        err = capsys.readouterr().err
        assert "Could not determine" in err
        assert "pip install --upgrade smolvm" in err

    def test_check_only_update_available(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("smolvm.cli.update._check_for_stable_update", return_value=("0.9.0", "1.0.0")),
        ):
            rc = run_update(check=True)
        assert rc == 0
        out = capsys.readouterr().out
        assert "1.0.0" in out

    def test_check_only_json_no_update(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("smolvm.cli.update._check_for_stable_update", return_value=("1.0.0", None)),
        ):
            rc = run_update(check=True, json_output=True)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["update_available"] is False

    def test_already_latest_skips_pip(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("smolvm.cli.update._check_for_stable_update", return_value=("1.0.0", None)),
            patch("smolvm.cli.update._run_upgrade") as mock_pip,
        ):
            rc = run_update()
        assert rc == 0
        mock_pip.assert_not_called()

    def test_upgrade_calls_pip(self) -> None:
        with (
            patch("smolvm.cli.update._check_for_stable_update", return_value=("0.9.0", "1.0.0")),
            patch("smolvm.cli.update._run_upgrade", return_value=(0, "")) as mock_pip,
            patch("smolvm.cli.update._get_current_version", return_value="1.0.0"),
        ):
            rc = run_update()
        assert rc == 0
        mock_pip.assert_called_once_with(json_output=False)

    def test_pip_failure_returns_nonzero(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("smolvm.cli.update._check_for_stable_update", return_value=("0.9.0", "1.0.0")),
            patch("smolvm.cli.update._run_upgrade", return_value=(1, "error output")),
            patch("smolvm.cli.update._get_current_version", return_value="0.9.0"),
        ):
            rc = run_update()
        assert rc == 1

    def test_upgrade_json_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("smolvm.cli.update._check_for_stable_update", return_value=("0.9.0", "1.0.0")),
            patch(
                "smolvm.cli.update._run_upgrade",
                return_value=(0, "Successfully installed smolvm-1.0.0"),
            ),
            patch("smolvm.cli.update._get_current_version", return_value="1.0.0"),
        ):
            rc = run_update(json_output=True)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["upgraded"] is True
        assert payload["data"]["current"] == "1.0.0"
        assert payload["data"]["previous"] == "0.9.0"
