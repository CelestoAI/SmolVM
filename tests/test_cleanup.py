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

"""Tests for delete and cleanup CLI commands."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.cli.cleanup import run_cleanup, run_delete, run_prune_sandboxes
from smolvm.cli.main import main as cli_main


def _make_vm(vm_id: str) -> MagicMock:
    vm = MagicMock()
    vm.vm_id = vm_id
    return vm


class TestDelete:
    """Tests for ``smolvm sandbox delete <vm-id>``."""

    @pytest.fixture
    def mock_sdk_cls(self) -> MagicMock:
        with patch("smolvm.cli.cleanup.CLIService") as mock_service_cls:
            sdk = MagicMock()
            manager = mock_service_cls.return_value.manager.return_value
            manager.__enter__.return_value = sdk
            manager.__exit__.return_value = None
            yield sdk

    @patch("smolvm.cli.cleanup.os.geteuid", return_value=0)
    def test_run_delete_single_vm(
        self,
        _: MagicMock,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Deleting a single VM by ID."""
        sdk = mock_sdk_cls

        ret = run_delete(vm_ids=["vm-abc123"])

        assert ret == 0
        sdk.delete.assert_called_once_with("vm-abc123")
        sdk.list_vms.assert_not_called()

    @patch("smolvm.cli.cleanup.os.geteuid", return_value=0)
    def test_run_delete_multiple_vms(
        self,
        _: MagicMock,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Deleting multiple VMs by ID."""
        sdk = mock_sdk_cls

        ret = run_delete(vm_ids=["vm-abc", "vm-def"])

        assert ret == 0
        assert sdk.delete.call_count == 2

    @patch("smolvm.cli.cleanup.os.geteuid", return_value=0)
    def test_run_delete_dry_run(
        self,
        _: MagicMock,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Dry run should not call delete."""
        sdk = mock_sdk_cls

        ret = run_delete(vm_ids=["vm-abc123"], dry_run=True)

        assert ret == 0
        sdk.delete.assert_not_called()
        out = capsys.readouterr().out
        assert "Dry run complete" in out

    @patch("smolvm.cli.cleanup.os.geteuid", return_value=0)
    def test_run_delete_partial_failure(
        self,
        _: MagicMock,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Partial failure should return exit code 1."""
        sdk = mock_sdk_cls

        def _delete(vm_id: str) -> None:
            if vm_id == "vm-def":
                raise RuntimeError("busy")

        sdk.delete.side_effect = _delete

        ret = run_delete(vm_ids=["vm-abc", "vm-def"])

        assert ret == 1
        out = capsys.readouterr().out
        assert "deleted" in out
        assert "failed" in out
        assert "busy" in out

    @patch("smolvm.cli.cleanup.os.geteuid", return_value=0)
    def test_run_delete_json(
        self,
        _: MagicMock,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """JSON output for delete."""

        ret = run_delete(vm_ids=["vm-abc"], json_output=True)

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "sandbox.delete"
        assert payload["ok"] is True
        assert payload["data"]["targets"] == ["vm-abc"]
        assert payload["data"]["deleted"] == ["vm-abc"]

    @patch("smolvm.cli.cleanup.run_delete", return_value=0)
    def test_cli_delete_forwards_args(self, mock_run_delete: MagicMock) -> None:
        """`smolvm sandbox delete vm-abc vm-def --json` forwards correctly."""
        ret = cli_main(["sandbox", "delete", "vm-abc", "vm-def", "--json"])

        assert ret == 0
        mock_run_delete.assert_called_once_with(
            vm_ids=["vm-abc", "vm-def"],
            dry_run=False,
            json_output=True,
            command_name="sandbox.delete",
        )


class TestCleanup:
    """Tests for ``smolvm sandbox delete --all``."""

    @pytest.fixture
    def mock_sdk_cls(self) -> MagicMock:
        with patch("smolvm.cli.cleanup.CLIService") as mock_service_cls:
            sdk = MagicMock()
            manager = mock_service_cls.return_value.manager.return_value
            manager.__enter__.return_value = sdk
            manager.__exit__.return_value = None
            yield sdk

    @patch("smolvm.cli.cleanup.os.geteuid", return_value=1000)
    @patch("smolvm.cli.cleanup.sys")
    def test_run_cleanup_dry_run_human(
        self,
        mock_sys: MagicMock,
        _: MagicMock,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Dry run should show warning on Linux, targets, and a summary."""
        mock_sys.platform = "linux"
        sdk = mock_sdk_cls
        sdk.reconcile.return_value = []
        sdk.list_vms.return_value = [_make_vm("vm-abc123"), _make_vm("vm-def456")]

        ret = run_cleanup(dry_run=True)

        assert ret == 0
        out = capsys.readouterr().out
        assert "Warning" in out
        assert "Targets (2)" in out
        assert "vm-abc123" in out
        assert "vm-def456" in out
        assert "Dry run complete" in out
        sdk.delete.assert_not_called()

    @patch("smolvm.cli.cleanup.os.geteuid", return_value=0)
    def test_run_cleanup_deletes_all(
        self,
        _: MagicMock,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Cleanup deletes all VMs."""
        sdk = mock_sdk_cls
        sdk.reconcile.return_value = []
        sdk.list_vms.return_value = [_make_vm("vm-abc123"), _make_vm("vm-def456")]

        ret = run_cleanup(force=True)

        assert ret == 0
        assert sdk.delete.call_count == 2

    @patch("smolvm.cli.cleanup.os.geteuid", return_value=0)
    def test_run_cleanup_partial_failure(
        self,
        _: MagicMock,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Cleanup should render failed deletions."""
        sdk = mock_sdk_cls
        sdk.reconcile.return_value = []
        sdk.list_vms.return_value = [_make_vm("vm-abc123"), _make_vm("vm-def456")]

        def _delete(vm_id: str) -> None:
            if vm_id == "vm-def456":
                raise RuntimeError("busy")

        sdk.delete.side_effect = _delete

        ret = run_cleanup(force=True)

        assert ret == 1
        out = capsys.readouterr().out
        assert "Delete Results" in out
        assert "deleted" in out
        assert "failed" in out
        assert "busy" in out

    @patch("smolvm.cli.cleanup.os.geteuid", return_value=0)
    def test_run_cleanup_json(
        self,
        _: MagicMock,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """JSON output for cleanup."""
        sdk = mock_sdk_cls
        sdk.reconcile.return_value = ["vm-stale"]
        sdk.list_vms.return_value = [_make_vm("vm-stale"), _make_vm("vm-other")]

        ret = run_cleanup(json_output=True, force=True)

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "sandbox.delete"
        assert payload["ok"] is True
        assert set(payload["data"]["targets"]) == {"vm-stale", "vm-other"}
        assert set(payload["data"]["deleted"]) == {"vm-stale", "vm-other"}
        assert payload["data"]["reconciled_stale_ids"] == ["vm-stale"]
        assert payload["data"]["summary"]["failed_count"] == 0

    @patch("smolvm.cli.cleanup.os.geteuid", return_value=0)
    def test_run_cleanup_json_requires_force(
        self,
        _: MagicMock,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """JSON mode without --force should refuse to delete and emit a JSON envelope."""
        sdk = mock_sdk_cls
        sdk.reconcile.return_value = []
        sdk.list_vms.return_value = [_make_vm("vm-abc123")]

        ret = run_cleanup(json_output=True)

        assert ret == 1
        sdk.delete.assert_not_called()
        payload = json.loads(capsys.readouterr().out)
        assert isinstance(payload, dict)
        assert payload["ok"] is False
        assert payload["command"] == "sandbox.delete"
        assert payload["exit_code"] == 1
        assert "force" in payload["error"]["message"].lower()
        assert "smolvm sandbox delete --all --force --json" in payload["error"]["message"]
        assert (
            payload["error"]["recovery"]
            == "Run 'smolvm sandbox delete --all --force --json' to confirm."
        )

    @patch("smolvm.cli.cleanup.os.geteuid", return_value=0)
    @patch("smolvm.cli.cleanup.sys.stdin")
    def test_run_cleanup_non_tty_requires_force(
        self,
        mock_stdin: MagicMock,
        _: MagicMock,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Non-TTY callers must pass --force; we won't silently delete."""
        mock_stdin.isatty.return_value = False
        sdk = mock_sdk_cls
        sdk.reconcile.return_value = []
        sdk.list_vms.return_value = [_make_vm("vm-abc123")]

        ret = run_cleanup()

        assert ret == 1
        sdk.delete.assert_not_called()

    @patch("smolvm.cli.cleanup.os.geteuid", return_value=0)
    @patch("smolvm.cli.cleanup.sys.stdin")
    @patch("smolvm.cli.cleanup.input", create=True)
    def test_run_cleanup_prompt_yes(
        self,
        mock_input: MagicMock,
        mock_stdin: MagicMock,
        _: MagicMock,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """TTY prompt with 'y' proceeds with deletion."""
        mock_stdin.isatty.return_value = True
        mock_input.return_value = "y"
        sdk = mock_sdk_cls
        sdk.reconcile.return_value = []
        sdk.list_vms.return_value = [_make_vm("vm-abc123")]

        ret = run_cleanup()

        assert ret == 0
        sdk.delete.assert_called_once_with("vm-abc123")

    @patch("smolvm.cli.cleanup.os.geteuid", return_value=0)
    @patch("smolvm.cli.cleanup.sys.stdin")
    @patch("smolvm.cli.cleanup.input", create=True)
    def test_run_cleanup_prompt_no(
        self,
        mock_input: MagicMock,
        mock_stdin: MagicMock,
        _: MagicMock,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """TTY prompt with empty/'n' aborts."""
        mock_stdin.isatty.return_value = True
        mock_input.return_value = ""
        sdk = mock_sdk_cls
        sdk.reconcile.return_value = []
        sdk.list_vms.return_value = [_make_vm("vm-abc123")]

        ret = run_cleanup()

        assert ret == 0
        sdk.delete.assert_not_called()
        assert "Aborted" in capsys.readouterr().out

    @patch("smolvm.cli.cleanup.run_cleanup", return_value=0)
    def test_cli_cleanup_forwards_json(self, mock_run_cleanup: MagicMock) -> None:
        """`smolvm sandbox delete --all --force --json` forwards correctly."""
        ret = cli_main(["sandbox", "delete", "--all", "--force", "--json"])

        assert ret == 0
        mock_run_cleanup.assert_called_once_with(
            dry_run=False,
            json_output=True,
            force=True,
            command_name="sandbox.delete",
        )

    @patch("smolvm.cli.cleanup.run_cleanup")
    def test_cli_cleanup_json_requires_force(
        self,
        mock_run_cleanup: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """`smolvm sandbox delete --all --json` is rejected before runtime."""
        ret = cli_main(["sandbox", "delete", "--all", "--json"])

        assert ret == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert payload["command"] == "sandbox.delete"
        assert payload["error"]["code"] == "refused"
        assert "smolvm sandbox delete --all --force --json" in payload["error"]["message"]
        assert (
            payload["error"]["recovery"]
            == "Run 'smolvm sandbox delete --all --force --json' to confirm."
        )
        mock_run_cleanup.assert_not_called()


class TestSandboxPrune:
    """Tests for ``smolvm sandbox prune``."""

    @pytest.fixture
    def mock_sdk_cls(self) -> MagicMock:
        with patch("smolvm.cli.cleanup.CLIService") as mock_service_cls:
            sdk = MagicMock()
            manager = mock_service_cls.return_value.manager.return_value
            manager.__enter__.return_value = sdk
            manager.__exit__.return_value = None
            yield sdk

    @staticmethod
    def _leftover(
        vm_id: str, name: str, *, kind: str = "disk", retained: bool = False
    ) -> MagicMock:
        artifact = MagicMock()
        artifact.vm_id = vm_id
        artifact.path = Path(f"/data/disks/{name}")
        artifact.kind = kind
        artifact.size_bytes = 1024
        artifact.retained = retained
        return artifact

    def test_prune_removes_leftovers(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Leftover files are deleted and the freed space is reported."""
        sdk = mock_sdk_cls
        leftover = self._leftover("sbx-gone", "sbx-gone.qcow2")
        sdk.find_leftover_artifacts.return_value = [leftover]
        sdk.prune_leftover_artifacts.return_value = [leftover]

        ret = run_prune_sandboxes(force=True)

        assert ret == 0
        sdk.prune_leftover_artifacts.assert_called_once_with(include_retained=False)
        out = capsys.readouterr().out
        assert "sbx-gone.qcow2" in out
        assert "Freed" in out

    def test_prune_dry_run_keeps_files(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """A dry run reports the same list without deleting anything."""
        sdk = mock_sdk_cls
        sdk.find_leftover_artifacts.return_value = [self._leftover("sbx-gone", "sbx-gone.qcow2")]

        ret = run_prune_sandboxes(dry_run=True)

        assert ret == 0
        sdk.prune_leftover_artifacts.assert_not_called()
        assert "Would free" in capsys.readouterr().out

    def test_prune_reports_saved_disks_it_skipped(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Saved disks are left alone, and the user is told how to remove them."""
        sdk = mock_sdk_cls
        sdk.find_leftover_artifacts.return_value = [
            self._leftover("sbx-saved", "sbx-saved.qcow2", retained=True)
        ]

        ret = run_prune_sandboxes(force=True)

        assert ret == 0
        sdk.prune_leftover_artifacts.assert_not_called()
        out = capsys.readouterr().out
        assert "--include-saved" in out
        assert "Nothing to clean up" in out

    def test_prune_include_saved_removes_them(
        self,
        mock_sdk_cls: MagicMock,
    ) -> None:
        """``--include-saved`` opts in to deleting kept disks."""
        sdk = mock_sdk_cls
        saved = self._leftover("sbx-saved", "sbx-saved.qcow2", retained=True)
        sdk.find_leftover_artifacts.return_value = [saved]
        sdk.prune_leftover_artifacts.return_value = [saved]

        ret = run_prune_sandboxes(force=True, include_retained=True)

        assert ret == 0
        sdk.prune_leftover_artifacts.assert_called_once_with(include_retained=True)

    def test_prune_json_requires_force(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Unattended JSON callers must confirm before anything is deleted."""
        sdk = mock_sdk_cls
        sdk.find_leftover_artifacts.return_value = [self._leftover("sbx-gone", "sbx-gone.qcow2")]

        ret = run_prune_sandboxes(json_output=True)

        assert ret == 1
        sdk.prune_leftover_artifacts.assert_not_called()
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert "smolvm sandbox prune --force --json" in payload["error"]["message"]

    def test_prune_json_payload(
        self,
        mock_sdk_cls: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """JSON output lists what was removed and how much it freed."""
        sdk = mock_sdk_cls
        leftover = self._leftover("sbx-gone", "sbx-gone.qcow2")
        sdk.find_leftover_artifacts.return_value = [leftover]
        sdk.prune_leftover_artifacts.return_value = [leftover]

        ret = run_prune_sandboxes(force=True, json_output=True)

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "sandbox.prune"
        assert payload["ok"] is True
        assert payload["data"]["removed"][0]["vm_id"] == "sbx-gone"
        assert payload["data"]["freed_bytes"] == 1024

    def test_cli_wires_prune_command(self) -> None:
        """``smolvm sandbox prune`` reaches the runner with its flags."""
        with patch("smolvm.cli.cleanup.run_prune_sandboxes", return_value=0) as runner:
            ret = cli_main(["sandbox", "prune", "--dry-run", "--include-saved"])

        assert ret == 0
        runner.assert_called_once_with(
            dry_run=True,
            force=False,
            include_retained=True,
            json_output=False,
        )
