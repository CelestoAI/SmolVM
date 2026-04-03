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

from smolvm.doctor import (
    DoctorCheck,
    DoctorReport,
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
        assert "SmolVM Doctor" in output
        assert "Checks" in output
        assert "strict mode treats warnings as failures" in output
        assert "\033[" not in output

    def test_run_doctor_json_envelope(self, capsys: pytest.CaptureFixture) -> None:
        """JSON doctor output should use the shared envelope."""
        report = DoctorReport(
            backend_requested="auto",
            backend_resolved="qemu",
            system="Darwin",
            arch="arm64",
            checks=[DoctorCheck(name="qemu", status="pass", detail="/usr/bin/qemu")],
        )

        with patch("smolvm.doctor.generate_doctor_report", return_value=report):
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

    @patch("smolvm.doctor.platform.system", return_value="Linux")
    @patch("smolvm.doctor.subprocess.run")
    @patch("smolvm.doctor.which")
    @patch(
        "smolvm.doctor._find_qemu_binary",
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

    @patch("smolvm.doctor.platform.system", return_value="Linux")
    @patch("smolvm.doctor.subprocess.run")
    @patch("smolvm.doctor.which")
    @patch(
        "smolvm.doctor._find_qemu_binary",
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

    @patch("smolvm.doctor.platform.system", return_value="Linux")
    @patch("smolvm.doctor.subprocess.run", side_effect=OSError("probe failed"))
    @patch("smolvm.doctor.which")
    @patch(
        "smolvm.doctor._find_qemu_binary",
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

    @patch("smolvm.doctor.platform.system", return_value="Linux")
    @patch("smolvm.doctor.subprocess.run", side_effect=RuntimeError("boom"))
    @patch("smolvm.doctor.which")
    @patch(
        "smolvm.doctor._find_qemu_binary",
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
