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

"""Host diagnostics for SmolVM (``smolvm doctor``)."""

from __future__ import annotations

import json
import platform
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from smolvm.backends import BACKEND_AUTO, BACKEND_FIRECRACKER, BACKEND_QEMU, resolve_backend
from smolvm.exceptions import SmolVMError
from smolvm.host import HostManager
from smolvm.network import check_network_prerequisites
from smolvm.utils import run_command, which

DoctorStatus = Literal["pass", "warn", "fail"]


@dataclass(frozen=True)
class DoctorCheck:
    """Single doctor check result."""

    name: str
    status: DoctorStatus
    detail: str


@dataclass(frozen=True)
class DoctorReport:
    """Structured diagnostics report."""

    backend_requested: str
    backend_resolved: str
    system: str
    arch: str
    checks: list[DoctorCheck]

    @property
    def failures(self) -> list[DoctorCheck]:
        return [check for check in self.checks if check.status == "fail"]

    @property
    def warnings(self) -> list[DoctorCheck]:
        return [check for check in self.checks if check.status == "warn"]


def _check_command(binary: str, package_hint: str) -> DoctorCheck:
    path = which(binary)
    if path is None:
        return DoctorCheck(
            name=f"command:{binary}",
            status="fail",
            detail=f"'{binary}' not found (install {package_hint})",
        )
    return DoctorCheck(
        name=f"command:{binary}",
        status="pass",
        detail=str(path),
    )


def _qemu_binary_candidates() -> list[str]:
    arch = platform.machine().lower()
    if arch in {"arm64", "aarch64"}:
        return ["qemu-system-aarch64", "qemu-system-x86_64"]
    if arch in {"x86_64", "amd64"}:
        return ["qemu-system-x86_64", "qemu-system-aarch64"]
    return ["qemu-system-aarch64", "qemu-system-x86_64"]


def _find_qemu_binary() -> tuple[str, Path] | None:
    for candidate in _qemu_binary_candidates():
        binary = which(candidate)
        if binary is not None:
            return candidate, binary
    return None


def _check_nft_table(family: str, table: str) -> DoctorCheck:
    try:
        run_command(["nft", "list", "table", family, table], use_sudo=True)
        return DoctorCheck(
            name=f"nft-table:{family}:{table}",
            status="pass",
            detail="exists",
        )
    except SmolVMError as e:
        msg = str(e).lower()
        if "no such file or directory" in msg or "does not exist" in msg:
            return DoctorCheck(
                name=f"nft-table:{family}:{table}",
                status="warn",
                detail="not created yet (will be created on first VM network setup)",
            )
        return DoctorCheck(
            name=f"nft-table:{family}:{table}",
            status="warn",
            detail=f"could not inspect table: {e}",
        )


def generate_doctor_report(backend: str | None = None) -> DoctorReport:
    """Collect diagnostics for the selected runtime backend."""
    requested = (backend or BACKEND_AUTO).strip().lower()
    resolved = resolve_backend(requested)

    checks: list[DoctorCheck] = []

    if resolved == BACKEND_FIRECRACKER:
        host = HostManager()

        kvm_ok = host.check_kvm()
        checks.append(
            DoctorCheck(
                name="kvm",
                status="pass" if kvm_ok else "fail",
                detail="/dev/kvm is available" if kvm_ok else "/dev/kvm unavailable",
            )
        )

        firecracker_path = host.find_firecracker()
        checks.append(
            DoctorCheck(
                name="firecracker",
                status="pass" if firecracker_path is not None else "fail",
                detail=(
                    str(firecracker_path)
                    if firecracker_path is not None
                    else "binary not found in PATH or ~/.smolvm/bin"
                ),
            )
        )

        checks.append(_check_command("ip", "iproute2"))
        checks.append(_check_command("nft", "nftables"))
        checks.append(_check_command("ssh", "openssh-client"))

        net_errors = check_network_prerequisites()
        if net_errors:
            checks.append(
                DoctorCheck(
                    name="network-permissions",
                    status="fail",
                    detail="; ".join(net_errors),
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="network-permissions",
                    status="pass",
                    detail="network commands and sudo policy are available",
                )
            )

        # Informational visibility into managed nftables objects.
        checks.append(_check_nft_table("ip", "smolvm_nat"))
        checks.append(_check_nft_table("inet", "smolvm_filter"))

    elif resolved == BACKEND_QEMU:
        qemu = _find_qemu_binary()
        if qemu is None:
            checks.append(
                DoctorCheck(
                    name="qemu",
                    status="fail",
                    detail=(
                        "QEMU not found. Install one of: qemu-system-aarch64, "
                        "qemu-system-x86_64"
                    ),
                )
            )
        else:
            qemu_name, qemu_path = qemu
            checks.append(
                DoctorCheck(
                    name="qemu",
                    status="pass",
                    detail=f"{qemu_name} ({qemu_path})",
                )
            )

            if platform.system() == "Darwin":
                try:
                    result = subprocess.run(
                        [str(qemu_path), "-accel", "help"],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=5,
                    )
                    accel_text = f"{result.stdout}\n{result.stderr}".lower()
                    if "hvf" in accel_text:
                        checks.append(
                            DoctorCheck(
                                name="qemu-accel",
                                status="pass",
                                detail="Hypervisor.framework (hvf) is available",
                            )
                        )
                    else:
                        checks.append(
                            DoctorCheck(
                                name="qemu-accel",
                                status="fail",
                                detail="hvf accelerator not reported by qemu",
                            )
                        )
                except Exception as e:
                    checks.append(
                        DoctorCheck(
                            name="qemu-accel",
                            status="warn",
                            detail=f"could not probe qemu accelerators: {e}",
                        )
                    )

        checks.append(_check_command("ssh", "openssh-client"))

    else:
        checks.append(
            DoctorCheck(
                name="backend",
                status="fail",
                detail=f"unsupported backend: {resolved}",
            )
        )

    return DoctorReport(
        backend_requested=requested,
        backend_resolved=resolved,
        system=platform.system(),
        arch=platform.machine(),
        checks=checks,
    )


def _print_human_report(report: DoctorReport, strict: bool) -> None:
    print("SmolVM Doctor")
    print(f"Backend: {report.backend_resolved} (requested: {report.backend_requested})")
    print(f"Platform: {report.system} {report.arch}")
    print("")

    markers = {
        "pass": "PASS",
        "warn": "WARN",
        "fail": "FAIL",
    }

    for check in report.checks:
        print(f"[{markers[check.status]}] {check.name}: {check.detail}")

    print("")
    failures = len(report.failures)
    warnings = len(report.warnings)
    if failures == 0 and (warnings == 0 or not strict):
        print("Doctor result: OK")
    elif strict and warnings and not failures:
        print("Doctor result: FAIL (strict mode treats warnings as failures)")
    else:
        print("Doctor result: FAIL")


def run_doctor(
    *,
    backend: str | None = None,
    json_output: bool = False,
    strict: bool = False,
) -> int:
    """Run host diagnostics and return a process-style exit code."""
    report = generate_doctor_report(backend=backend)

    failures = len(report.failures)
    warnings = len(report.warnings)
    exit_code = 1 if failures > 0 or (strict and warnings > 0) else 0

    if json_output:
        payload = {
            "backend_requested": report.backend_requested,
            "backend_resolved": report.backend_resolved,
            "system": report.system,
            "arch": report.arch,
            "checks": [asdict(check) for check in report.checks],
            "summary": {
                "failures": failures,
                "warnings": warnings,
                "ok": exit_code == 0,
                "strict": strict,
            },
        }
        print(json.dumps(payload, indent=2))
    else:
        _print_human_report(report, strict=strict)

    return exit_code
