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

"""Tests for delete CLI rendering and JSON output."""

import json
from unittest.mock import MagicMock, patch

import pytest

from smolvm.cleanup import build_parser, main as delete_main, run_delete
from smolvm.cli import main as cli_main


def _make_vm(vm_id: str) -> MagicMock:
    vm = MagicMock()
    vm.vm_id = vm_id
    return vm


class TestDelete:
    """Tests for delete output contracts."""

    @pytest.fixture
    def mock_sdk_cls(self) -> MagicMock:
        with patch("smolvm.cleanup.SmolVMManager") as mock_cls:
            sdk = MagicMock()
            mock_cls.return_value.__enter__.return_value = sdk
            mock_cls.return_value.__exit__.return_value = None
            yield sdk

    @patch("smolvm.cleanup.os.geteuid", return_value=1000)
    @patch("smolvm.cleanup.sys")
    def test_run_delete_dry_run_human(
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

        ret = run_delete(prefix="vm-", dry_run=True)

        assert ret == 0
        out = capsys.readouterr().out
        assert "Warning" in out
        assert "Targets (2)" in out
        assert "vm-abc123" in out
        assert "vm-def456" in out
        assert "Dry run complete. No changes made." in out
        sdk.delete.assert_not_called()

    @patch("smolvm.cleanup.os.geteuid", return_value=0)
    def test_run_delete_partial_failure_human(
        self,
        _: MagicMock,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Delete should render failed deletions in the results table."""
        sdk = mock_sdk_cls
        sdk.reconcile.return_value = []
        sdk.list_vms.return_value = [_make_vm("vm-abc123"), _make_vm("vm-def456")]

        def _delete(vm_id: str) -> None:
            if vm_id == "vm-def456":
                raise RuntimeError("busy")

        sdk.delete.side_effect = _delete

        ret = run_delete(delete_all=True)

        assert ret == 1
        out = capsys.readouterr().out
        assert "Delete Results" in out
        assert "deleted" in out
        assert "failed" in out
        assert "busy" in out

    @patch("smolvm.cleanup.os.geteuid", return_value=0)
    def test_run_delete_json(
        self,
        _: MagicMock,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`run_delete(..., json_output=True)` should emit the shared envelope."""
        sdk = mock_sdk_cls
        sdk.reconcile.return_value = ["vm-stale"]
        sdk.list_vms.return_value = [_make_vm("vm-stale"), _make_vm("vm-other")]

        ret = run_delete(delete_all=True, json_output=True)

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "delete"
        assert payload["ok"] is True
        assert payload["data"]["reconciled_stale_ids"] == ["vm-stale"]
        assert set(payload["data"]["targets"]) == {"vm-stale", "vm-other"}
        assert set(payload["data"]["deleted"]) == {"vm-stale", "vm-other"}
        assert payload["data"]["summary"]["failed_count"] == 0

    def test_delete_parser_includes_json(self) -> None:
        """The standalone delete parser should expose `--json`."""
        args = build_parser().parse_args(["--json"])

        assert args.json is True

    @patch("smolvm.cli.run_delete", return_value=0)
    def test_cli_delete_forwards_json(self, mock_run_delete: MagicMock) -> None:
        """`smolvm delete --json` should forward the JSON flag."""
        ret = cli_main(["delete", "--json"])

        assert ret == 0
        mock_run_delete.assert_called_once_with(
            vm_ids=None,
            delete_all=False,
            prefix="vm-",
            dry_run=False,
            json_output=True,
        )

    @patch("smolvm.cleanup.run_delete", return_value=0)
    def test_standalone_delete_main_forwards_json(self, mock_run_delete: MagicMock) -> None:
        """`smolvm-delete --json` should forward the JSON flag."""
        ret = delete_main(["--json"])

        assert ret == 0
        mock_run_delete.assert_called_once_with(
            vm_ids=None,
            delete_all=False,
            prefix="vm-",
            dry_run=False,
            json_output=True,
        )

    @patch("smolvm.cli.run_delete", return_value=0)
    def test_cli_delete_with_vm_ids(self, mock_run_delete: MagicMock) -> None:
        """`smolvm delete vm-abc vm-def` should forward vm_ids."""
        ret = cli_main(["delete", "vm-abc", "vm-def"])

        assert ret == 0
        mock_run_delete.assert_called_once_with(
            vm_ids=["vm-abc", "vm-def"],
            delete_all=False,
            prefix="vm-",
            dry_run=False,
            json_output=False,
        )

    @patch("smolvm.cleanup.os.geteuid", return_value=0)
    def test_run_delete_explicit_vm_ids(
        self,
        _: MagicMock,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Explicit vm_ids should delete exactly those VMs."""
        sdk = mock_sdk_cls
        sdk.reconcile.return_value = []

        ret = run_delete(vm_ids=["vm-abc123"])

        assert ret == 0
        sdk.delete.assert_called_once_with("vm-abc123")
        sdk.list_vms.assert_not_called()
