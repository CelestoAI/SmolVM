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

"""Tests for cleanup CLI rendering and JSON output."""

import json
from unittest.mock import MagicMock, patch

import pytest

from smolvm.cleanup import build_parser, main as cleanup_main, run_cleanup
from smolvm.cli import main as cli_main


def _make_vm(vm_id: str) -> MagicMock:
    vm = MagicMock()
    vm.vm_id = vm_id
    return vm


class TestCleanup:
    """Tests for cleanup output contracts."""

    @pytest.fixture
    def mock_sdk_cls(self) -> MagicMock:
        with patch("smolvm.cleanup.SmolVMManager") as mock_cls:
            sdk = MagicMock()
            mock_cls.return_value.__enter__.return_value = sdk
            mock_cls.return_value.__exit__.return_value = None
            yield sdk

    @patch("smolvm.cleanup.os.geteuid", return_value=1000)
    @patch("smolvm.cleanup.sys")
    def test_run_cleanup_dry_run_human(
        self,
        mock_sys: MagicMock,
        _: MagicMock,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Dry runs should show warning on Linux, targets, and a summary."""
        mock_sys.platform = "linux"
        sdk = mock_sdk_cls
        sdk.reconcile.return_value = []
        sdk.list_vms.return_value = [_make_vm("vm-abc123"), _make_vm("vm-def456")]

        ret = run_cleanup(prefix="vm-", dry_run=True)

        assert ret == 0
        out = capsys.readouterr().out
        assert "Warning" in out
        assert "Targets (2)" in out
        assert "vm-abc123" in out
        assert "vm-def456" in out
        assert "Dry run complete. No changes made." in out
        sdk.delete.assert_not_called()

    @patch("smolvm.cleanup.os.geteuid", return_value=0)
    def test_run_cleanup_partial_failure_human(
        self,
        _: MagicMock,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Cleanup should render failed deletions in the human results table."""
        sdk = mock_sdk_cls
        sdk.reconcile.return_value = []
        sdk.list_vms.return_value = [_make_vm("vm-abc123"), _make_vm("vm-def456")]

        def _delete(vm_id: str) -> None:
            if vm_id == "vm-def456":
                raise RuntimeError("busy")

        sdk.delete.side_effect = _delete

        ret = run_cleanup(delete_all=True)

        assert ret == 1
        out = capsys.readouterr().out
        assert "Cleanup Results" in out
        assert "deleted" in out
        assert "failed" in out
        assert "busy" in out

    @patch("smolvm.cleanup.os.geteuid", return_value=0)
    def test_run_cleanup_json(
        self,
        _: MagicMock,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`run_cleanup(..., json_output=True)` should emit the shared envelope."""
        sdk = mock_sdk_cls
        sdk.reconcile.return_value = ["vm-stale"]
        sdk.list_vms.return_value = [_make_vm("vm-stale"), _make_vm("vm-other")]

        ret = run_cleanup(delete_all=True, json_output=True)

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "cleanup"
        assert payload["ok"] is True
        assert payload["data"]["reconciled_stale_ids"] == ["vm-stale"]
        assert payload["data"]["targets"] == ["vm-stale", "vm-other"]
        assert payload["data"]["deleted"] == ["vm-stale", "vm-other"]
        assert payload["data"]["summary"]["failed_count"] == 0

    def test_cleanup_parser_includes_json(self) -> None:
        """The standalone cleanup parser should expose `--json`."""
        args = build_parser().parse_args(["--json"])

        assert args.json is True

    @patch("smolvm.cli.run_cleanup", return_value=0)
    def test_cli_cleanup_forwards_json(self, mock_run_cleanup: MagicMock) -> None:
        """`smolvm cleanup --json` should forward the JSON flag."""
        ret = cli_main(["cleanup", "--json"])

        assert ret == 0
        mock_run_cleanup.assert_called_once_with(
            delete_all=False,
            prefix="vm-",
            dry_run=False,
            json_output=True,
        )

    @patch("smolvm.cleanup.run_cleanup", return_value=0)
    def test_standalone_cleanup_main_forwards_json(self, mock_run_cleanup: MagicMock) -> None:
        """`smolvm-cleanup --json` should forward the JSON flag."""
        ret = cleanup_main(["--json"])

        assert ret == 0
        mock_run_cleanup.assert_called_once_with(
            delete_all=False,
            prefix="vm-",
            dry_run=False,
            json_output=True,
        )
