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

"""Tests for SmolVM doctor diagnostics."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.exceptions import SmolVMError
from smolvm.host.doctor import (
    DoctorCheck,
    DoctorReport,
    WorkerNodeSecurityError,
    check_worker_node_security,
    generate_doctor_report,
    run_doctor,
)


def _pass(name: str) -> DoctorCheck:
    return DoctorCheck(name=name, status="pass", detail="ok")


class TestDoctorFirecracker:
    """Firecracker backend diagnostic tests."""

    @patch("smolvm.host.doctor._check_kvm_runtime", new=lambda: _pass("kvm"))
    @patch("smolvm.host.doctor._check_kvm_permissions", new=lambda: _pass("worker:kvm-permissions"))
    @patch(
        "smolvm.host.doctor._check_kvm_nx_huge_pages", new=lambda: _pass("worker:kvm-nx-huge-pages")
    )
    @patch("smolvm.host.doctor._check_thp_disabled", new=lambda: _pass("worker:thp-disabled"))
    @patch("smolvm.host.doctor._check_ksm_disabled", new=lambda: _pass("worker:ksm-disabled"))
    @patch("smolvm.host.doctor._check_swap_disabled", new=lambda: _pass("worker:swap-disabled"))
    @patch("smolvm.host.doctor.run_command")
    @patch("smolvm.host.doctor.check_network_prerequisites", return_value=[])
    @patch("smolvm.host.doctor.which")
    @patch("smolvm.host.doctor.HostManager")
    def test_generate_report_firecracker_ok(
        self,
        mock_host_cls: MagicMock,
        mock_which: MagicMock,
        mock_net_checks: MagicMock,
        mock_run_command: MagicMock,
    ) -> None:
        """A healthy firecracker setup should produce no failures."""
        mock_host = MagicMock()
        mock_host.check_kvm.return_value = True
        mock_host.find_firecracker.return_value = Path("/usr/local/bin/firecracker")
        mock_host_cls.return_value = mock_host

        def _which_side_effect(binary: str) -> Path | None:
            if binary in {"ip", "nft", "ssh"}:
                return Path(f"/usr/bin/{binary}")
            return None

        mock_which.side_effect = _which_side_effect
        mock_run_command.return_value = MagicMock(stdout="")

        report = generate_doctor_report(backend="firecracker")

        assert report.backend_resolved == "firecracker"
        assert report.failures == []
        assert report.warnings == []

    @patch("smolvm.host.doctor._check_kvm_runtime", new=lambda: _pass("kvm"))
    @patch("smolvm.host.doctor._check_kvm_permissions", new=lambda: _pass("worker:kvm-permissions"))
    @patch(
        "smolvm.host.doctor._check_kvm_nx_huge_pages", new=lambda: _pass("worker:kvm-nx-huge-pages")
    )
    @patch(
        "smolvm.host.doctor._check_thp_disabled",
        new=lambda: DoctorCheck(
            name="worker:thp-disabled",
            status="fail",
            detail="Active ('madvise')",
            fix="sudo sh -c 'echo never > /sys/kernel/mm/transparent_hugepage/enabled'",
        ),
    )
    @patch("smolvm.host.doctor._check_ksm_disabled", new=lambda: _pass("worker:ksm-disabled"))
    @patch(
        "smolvm.host.doctor._check_swap_disabled",
        new=lambda: DoctorCheck(
            name="worker:swap-disabled",
            status="fail",
            detail="Active (8388604 kB)",
            fix="sudo swapoff -a",
        ),
    )
    @patch("smolvm.host.doctor.run_command")
    @patch("smolvm.host.doctor.check_network_prerequisites", return_value=[])
    @patch("smolvm.host.doctor.which")
    @patch("smolvm.host.doctor.HostManager")
    def test_generate_report_firecracker_warns_for_worker_hardening_gaps(
        self,
        mock_host_cls: MagicMock,
        mock_which: MagicMock,
        mock_net_checks: MagicMock,
        mock_run_command: MagicMock,
    ) -> None:
        """Doctor should warn on worker hardening gaps during local evaluation."""
        mock_host = MagicMock()
        mock_host.check_kvm.return_value = True
        mock_host.find_firecracker.return_value = Path("/usr/local/bin/firecracker")
        mock_host_cls.return_value = mock_host

        def _which_side_effect(binary: str) -> Path | None:
            if binary in {"ip", "nft", "ssh"}:
                return Path(f"/usr/bin/{binary}")
            return None

        mock_which.side_effect = _which_side_effect
        mock_run_command.return_value = MagicMock(stdout="")

        report = generate_doctor_report(backend="firecracker")
        checks = {check.name: check for check in report.checks}

        assert checks["worker:swap-disabled"].status == "warn"
        assert checks["worker:thp-disabled"].status == "warn"
        assert report.failures == []
        assert {check.name for check in report.warnings} >= {
            "worker:swap-disabled",
            "worker:thp-disabled",
        }

    @patch("smolvm.host.doctor._check_kvm_runtime", new=lambda: _pass("kvm"))
    @patch("smolvm.host.doctor._check_kvm_permissions", new=lambda: _pass("worker:kvm-permissions"))
    @patch(
        "smolvm.host.doctor._check_kvm_nx_huge_pages", new=lambda: _pass("worker:kvm-nx-huge-pages")
    )
    @patch("smolvm.host.doctor._check_thp_disabled", new=lambda: _pass("worker:thp-disabled"))
    @patch("smolvm.host.doctor._check_ksm_disabled", new=lambda: _pass("worker:ksm-disabled"))
    @patch("smolvm.host.doctor._check_swap_disabled", new=lambda: _pass("worker:swap-disabled"))
    @patch("smolvm.host.doctor.run_command", side_effect=SmolVMError("No such file or directory"))
    @patch("smolvm.host.doctor.check_network_prerequisites", return_value=[])
    @patch("smolvm.host.doctor.which")
    @patch("smolvm.host.doctor.HostManager")
    def test_run_doctor_strict_fails_on_warnings(
        self,
        mock_host_cls: MagicMock,
        mock_which: MagicMock,
        mock_net_checks: MagicMock,
        mock_run_command: MagicMock,
        capsys,
    ) -> None:
        """Missing nft tables are warnings by default but fail in strict mode."""
        mock_host = MagicMock()
        mock_host.check_kvm.return_value = True
        mock_host.find_firecracker.return_value = Path("/usr/local/bin/firecracker")
        mock_host_cls.return_value = mock_host

        def _which_side_effect(binary: str) -> Path | None:
            if binary in {"ip", "nft", "ssh"}:
                return Path(f"/usr/bin/{binary}")
            return None

        mock_which.side_effect = _which_side_effect

        ret_normal = run_doctor(backend="firecracker", strict=False)
        ret_strict = run_doctor(backend="firecracker", strict=True)

        assert ret_normal == 0
        assert ret_strict == 1
        output = capsys.readouterr().out
        assert "SmolVM Doctor" in output
        assert "Checks" in output
        assert "strict mode treats warnings as failures" in output
        assert "\033[" not in output

    @patch("smolvm.host.doctor._check_kvm_runtime", new=lambda: _pass("kvm"))
    @patch("smolvm.host.doctor._check_kvm_permissions", new=lambda: _pass("worker:kvm-permissions"))
    @patch(
        "smolvm.host.doctor._check_kvm_nx_huge_pages", new=lambda: _pass("worker:kvm-nx-huge-pages")
    )
    @patch(
        "smolvm.host.doctor._check_thp_disabled",
        new=lambda: DoctorCheck(
            name="worker:thp-disabled",
            status="fail",
            detail="Active ('madvise')",
        ),
    )
    @patch("smolvm.host.doctor._check_ksm_disabled", new=lambda: _pass("worker:ksm-disabled"))
    @patch(
        "smolvm.host.doctor._check_swap_disabled",
        new=lambda: DoctorCheck(
            name="worker:swap-disabled",
            status="fail",
            detail="Active (8388604 kB)",
        ),
    )
    @patch("smolvm.host.doctor.run_command")
    @patch("smolvm.host.doctor.check_network_prerequisites", return_value=[])
    @patch("smolvm.host.doctor.which")
    @patch("smolvm.host.doctor.HostManager")
    def test_run_doctor_strict_fails_on_worker_hardening_warnings(
        self,
        mock_host_cls: MagicMock,
        mock_which: MagicMock,
        mock_net_checks: MagicMock,
        mock_run_command: MagicMock,
    ) -> None:
        """Strict mode should still fail on worker-node hardening warnings."""
        mock_host = MagicMock()
        mock_host.check_kvm.return_value = True
        mock_host.find_firecracker.return_value = Path("/usr/local/bin/firecracker")
        mock_host_cls.return_value = mock_host

        def _which_side_effect(binary: str) -> Path | None:
            if binary in {"ip", "nft", "ssh"}:
                return Path(f"/usr/bin/{binary}")
            return None

        mock_which.side_effect = _which_side_effect
        mock_run_command.return_value = MagicMock(stdout="")

        assert run_doctor(backend="firecracker", strict=False) == 0
        assert run_doctor(backend="firecracker", strict=True) == 1

    def test_run_doctor_json_envelope(self, capsys: pytest.CaptureFixture) -> None:
        """JSON doctor output should use the shared envelope."""
        report = DoctorReport(
            backend_requested="auto",
            backend_resolved="qemu",
            system="Darwin",
            arch="arm64",
            checks=[DoctorCheck(name="qemu", status="pass", detail="/usr/bin/qemu")],
        )

        with patch("smolvm.host.doctor.generate_doctor_report", return_value=report):
            ret = run_doctor(json_output=True, strict=False)

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "doctor"
        assert payload["ok"] is True
        assert payload["data"]["backend_resolved"] == "qemu"
        assert payload["data"]["checks"][0]["name"] == "qemu"
        assert payload["data"]["summary"]["ok"] is True


class TestDoctorQemu:
    """QEMU backend diagnostic tests."""

    @patch("smolvm.host.doctor._find_qemu_binary", return_value=None)
    @patch("smolvm.host.doctor.which", return_value=Path("/usr/bin/ssh"))
    def test_generate_report_qemu_missing_binary(
        self,
        mock_which: MagicMock,
        mock_find_qemu: MagicMock,
    ) -> None:
        """Missing qemu binary should produce a failure."""
        report = generate_doctor_report(backend="qemu")

        assert report.backend_resolved == "qemu"
        assert any(check.name == "qemu" and check.status == "fail" for check in report.checks)

    @patch("smolvm.host.doctor.platform.system", return_value="Linux")
    @patch("smolvm.host.doctor.subprocess.run")
    @patch("smolvm.host.doctor.which")
    @patch(
        "smolvm.host.doctor._find_qemu_binary",
        return_value=("qemu-system-x86_64", Path("/usr/bin/qemu-system-x86_64")),
    )
    def test_generate_report_qemu_ok_with_qemu_img_and_supported_version(
        self,
        _mock_find_qemu: MagicMock,
        mock_which: MagicMock,
        mock_run: MagicMock,
        _mock_system: MagicMock,
    ) -> None:
        """QEMU doctor should pass when qemu-img exists and QEMU is new enough."""
        mock_which.side_effect = lambda binary: Path(f"/usr/bin/{binary}")
        mock_run.return_value = MagicMock(stdout="QEMU emulator version 8.2.0", stderr="")

        report = generate_doctor_report(backend="qemu")
        checks = {check.name: check for check in report.checks}

        assert checks["qemu-version"].status == "pass"
        assert checks["command:qemu-img"].status == "pass"

    @patch("smolvm.host.doctor.platform.system", return_value="Linux")
    @patch("smolvm.host.doctor.subprocess.run")
    @patch("smolvm.host.doctor.which")
    @patch(
        "smolvm.host.doctor._find_qemu_binary",
        return_value=("qemu-system-x86_64", Path("/usr/bin/qemu-system-x86_64")),
    )
    def test_generate_report_qemu_fails_for_missing_qemu_img_and_old_version(
        self,
        _mock_find_qemu: MagicMock,
        mock_which: MagicMock,
        mock_run: MagicMock,
        _mock_system: MagicMock,
    ) -> None:
        """QEMU doctor should fail when qemu-img is missing or QEMU is too old."""
        mock_which.side_effect = lambda binary: Path("/usr/bin/ssh") if binary == "ssh" else None
        mock_run.return_value = MagicMock(stdout="QEMU emulator version 5.2.0", stderr="")

        report = generate_doctor_report(backend="qemu")
        checks = {check.name: check for check in report.checks}

        assert checks["qemu-version"].status == "fail"
        assert checks["command:qemu-img"].status == "fail"

    @patch("smolvm.host.doctor.platform.system", return_value="Linux")
    @patch("smolvm.host.doctor.subprocess.run", side_effect=OSError("probe failed"))
    @patch("smolvm.host.doctor.which")
    @patch(
        "smolvm.host.doctor._find_qemu_binary",
        return_value=("qemu-system-x86_64", Path("/usr/bin/qemu-system-x86_64")),
    )
    def test_generate_report_qemu_warns_when_version_probe_fails(
        self,
        _mock_find_qemu: MagicMock,
        mock_which: MagicMock,
        mock_run: MagicMock,
        _mock_system: MagicMock,
    ) -> None:
        """Expected subprocess probe failures should produce a warning."""
        mock_which.side_effect = lambda binary: Path(f"/usr/bin/{binary}")

        report = generate_doctor_report(backend="qemu")
        checks = {check.name: check for check in report.checks}

        assert checks["qemu-version"].status == "warn"
        assert "probe failed" in checks["qemu-version"].detail

    @patch("smolvm.host.doctor.platform.system", return_value="Linux")
    @patch("smolvm.host.doctor.subprocess.run", side_effect=RuntimeError("boom"))
    @patch("smolvm.host.doctor.which")
    @patch(
        "smolvm.host.doctor._find_qemu_binary",
        return_value=("qemu-system-x86_64", Path("/usr/bin/qemu-system-x86_64")),
    )
    def test_generate_report_qemu_propagates_unexpected_probe_errors(
        self,
        _mock_find_qemu: MagicMock,
        mock_which: MagicMock,
        mock_run: MagicMock,
        _mock_system: MagicMock,
    ) -> None:
        """Unexpected probe errors should propagate rather than being swallowed."""
        mock_which.side_effect = lambda binary: Path(f"/usr/bin/{binary}")

        with pytest.raises(RuntimeError, match="boom"):
            generate_doctor_report(backend="qemu")


class TestKvmRuntimeCheck:
    """Tests for the user-facing kvm doctor row."""

    @patch("smolvm.host.doctor._KVM_DEV")
    def test_missing_dev_kvm_fails_with_kvm_host_fix(self, mock_dev: MagicMock) -> None:
        from smolvm.host.doctor import _check_kvm_runtime

        mock_dev.exists.return_value = False
        result = _check_kvm_runtime()

        assert result.status == "fail"
        assert "not found" in result.detail
        assert result.fix is not None and "hardware virtualization" in result.fix

    @patch("smolvm.host.doctor._user_is_pending_kvm_group", return_value=False)
    @patch("smolvm.host.doctor.os.access", return_value=False)
    @patch("smolvm.host.doctor._KVM_DEV")
    def test_inaccessible_dev_kvm_fails_with_usermod_fix(
        self,
        mock_dev: MagicMock,
        _mock_access: MagicMock,
        _mock_pending: MagicMock,
    ) -> None:
        from smolvm.host.doctor import _check_kvm_runtime

        mock_dev.exists.return_value = True
        result = _check_kvm_runtime()

        assert result.status == "fail"
        assert "can't read or write" in result.detail
        assert result.fix is not None
        assert "usermod -aG kvm" in result.fix
        assert "newgrp kvm" in result.fix

    @patch("smolvm.host.doctor._user_is_pending_kvm_group", return_value=True)
    @patch("smolvm.host.doctor.os.access", return_value=False)
    @patch("smolvm.host.doctor._KVM_DEV")
    def test_pending_kvm_group_session_fails_with_relog_fix(
        self,
        mock_dev: MagicMock,
        _mock_access: MagicMock,
        _mock_pending: MagicMock,
    ) -> None:
        """User added to kvm group but current shell hasn't picked it up."""
        from smolvm.host.doctor import _check_kvm_runtime

        mock_dev.exists.return_value = True
        result = _check_kvm_runtime()

        assert result.status == "fail"
        assert "in the kvm group" in result.detail
        assert "predates the change" in result.detail
        assert result.fix is not None
        assert "Log out" in result.fix
        assert "sg kvm" in result.fix

    @patch("smolvm.host.doctor.os.access", return_value=True)
    @patch("smolvm.host.doctor._KVM_DEV")
    def test_accessible_dev_kvm_passes(
        self,
        mock_dev: MagicMock,
        _mock_access: MagicMock,
    ) -> None:
        from smolvm.host.doctor import _check_kvm_runtime

        mock_dev.exists.return_value = True
        result = _check_kvm_runtime()

        assert result.status == "pass"


class TestWorkerNodeSecurityChecks:
    """Tests for strict worker-node startup guard behavior."""

    @patch("smolvm.host.doctor._check_kvm_permissions", new=lambda: _pass("worker:kvm-permissions"))
    @patch(
        "smolvm.host.doctor._check_kvm_nx_huge_pages",
        new=lambda: DoctorCheck(
            name="worker:kvm-nx-huge-pages",
            status="warn",
            detail="kvm module not loaded",
        ),
    )
    @patch("smolvm.host.doctor._check_thp_disabled", new=lambda: _pass("worker:thp-disabled"))
    @patch("smolvm.host.doctor._check_ksm_disabled", new=lambda: _pass("worker:ksm-disabled"))
    @patch("smolvm.host.doctor._check_swap_disabled", new=lambda: _pass("worker:swap-disabled"))
    def test_check_worker_node_security_raises_on_warn(self) -> None:
        """Startup guard should reject non-pass security checks, including warnings."""
        with pytest.raises(WorkerNodeSecurityError, match=r"worker:kvm-nx-huge-pages \(warn\)"):
            check_worker_node_security()
