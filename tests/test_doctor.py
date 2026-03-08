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

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.doctor import (
    DoctorCheck,
    WorkerNodeSecurityError,
    check_worker_node_security,
    generate_doctor_report,
    run_doctor,
)
from smolvm.exceptions import SmolVMError


def _pass(name: str) -> DoctorCheck:
    return DoctorCheck(name=name, status="pass", detail="ok")


class TestDoctorFirecracker:
    """Firecracker backend diagnostic tests."""

    @patch("smolvm.doctor._check_kvm_permissions", new=lambda: _pass("worker:kvm-permissions"))
    @patch("smolvm.doctor._check_kvm_nx_huge_pages", new=lambda: _pass("worker:kvm-nx-huge-pages"))
    @patch("smolvm.doctor._check_thp_disabled", new=lambda: _pass("worker:thp-disabled"))
    @patch("smolvm.doctor._check_ksm_disabled", new=lambda: _pass("worker:ksm-disabled"))
    @patch("smolvm.doctor._check_swap_disabled", new=lambda: _pass("worker:swap-disabled"))
    @patch("smolvm.doctor.run_command")
    @patch("smolvm.doctor.check_network_prerequisites", return_value=[])
    @patch("smolvm.doctor.which")
    @patch("smolvm.doctor.HostManager")
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

    @patch("smolvm.doctor._check_kvm_permissions", new=lambda: _pass("worker:kvm-permissions"))
    @patch("smolvm.doctor._check_kvm_nx_huge_pages", new=lambda: _pass("worker:kvm-nx-huge-pages"))
    @patch("smolvm.doctor._check_thp_disabled", new=lambda: _pass("worker:thp-disabled"))
    @patch("smolvm.doctor._check_ksm_disabled", new=lambda: _pass("worker:ksm-disabled"))
    @patch("smolvm.doctor._check_swap_disabled", new=lambda: _pass("worker:swap-disabled"))
    @patch("smolvm.doctor.run_command", side_effect=SmolVMError("No such file or directory"))
    @patch("smolvm.doctor.check_network_prerequisites", return_value=[])
    @patch("smolvm.doctor.which")
    @patch("smolvm.doctor.HostManager")
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
        assert "strict mode treats warnings as failures" in output


class TestDoctorQemu:
    """QEMU backend diagnostic tests."""

    @patch("smolvm.doctor._find_qemu_binary", return_value=None)
    @patch("smolvm.doctor.which", return_value=Path("/usr/bin/ssh"))
    def test_generate_report_qemu_missing_binary(
        self,
        mock_which: MagicMock,
        mock_find_qemu: MagicMock,
    ) -> None:
        """Missing qemu binary should produce a failure."""
        report = generate_doctor_report(backend="qemu")

        assert report.backend_resolved == "qemu"
        assert any(check.name == "qemu" and check.status == "fail" for check in report.checks)


class TestWorkerNodeSecurityChecks:
    """Tests for strict worker-node startup guard behavior."""

    @patch("smolvm.doctor._check_kvm_permissions", new=lambda: _pass("worker:kvm-permissions"))
    @patch(
        "smolvm.doctor._check_kvm_nx_huge_pages",
        new=lambda: DoctorCheck(
            name="worker:kvm-nx-huge-pages",
            status="warn",
            detail="kvm module not loaded",
        ),
    )
    @patch("smolvm.doctor._check_thp_disabled", new=lambda: _pass("worker:thp-disabled"))
    @patch("smolvm.doctor._check_ksm_disabled", new=lambda: _pass("worker:ksm-disabled"))
    @patch("smolvm.doctor._check_swap_disabled", new=lambda: _pass("worker:swap-disabled"))
    def test_check_worker_node_security_raises_on_warn(self) -> None:
        """Startup guard should reject non-pass security checks, including warnings."""
        with pytest.raises(WorkerNodeSecurityError, match=r"worker:kvm-nx-huge-pages \(warn\)"):
            check_worker_node_security()
