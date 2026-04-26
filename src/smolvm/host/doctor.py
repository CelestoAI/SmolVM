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

import os
import platform
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from smolvm.cli.output import console_stdout, emit_json, render_error, status_style
from smolvm.exceptions import SmolVMError
from smolvm.host.manager import HostManager
from smolvm.host.network import check_network_prerequisites
from smolvm.runtime.backends import (
    BACKEND_AUTO,
    BACKEND_FIRECRACKER,
    BACKEND_LIBKRUN,
    BACKEND_QEMU,
    resolve_backend,
)
from smolvm.utils import run_command, which

DoctorStatus = Literal["pass", "warn", "fail"]


@dataclass(frozen=True)
class DoctorCheck:
    """Single doctor check result."""

    name: str
    status: DoctorStatus
    detail: str
    fix: str | None = None


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
            detail="Not found",
            fix=f"Install {package_hint}",
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


def _check_qemu_version(qemu_path: Path) -> DoctorCheck:
    """Check that the selected QEMU binary supports snapshot QMP APIs."""
    try:
        result = subprocess.run(
            [str(qemu_path), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except subprocess.TimeoutExpired as exc:
        return DoctorCheck(
            name="qemu-version",
            status="warn",
            detail=f"could not probe QEMU version: {exc}",
        )
    except (FileNotFoundError, OSError) as exc:
        return DoctorCheck(
            name="qemu-version",
            status="warn",
            detail=f"could not probe QEMU version: {exc}",
        )

    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", f"{result.stdout}\n{result.stderr}")
    if match is None:
        return DoctorCheck(
            name="qemu-version",
            status="warn",
            detail="could not parse QEMU version output",
        )

    major, minor, micro = match.groups()
    version = f"{major}.{minor}.{micro or '0'}"
    if (int(major), int(minor), int(micro or 0)) < (6, 0, 0):
        return DoctorCheck(
            name="qemu-version",
            status="fail",
            detail=f"{version} detected",
            fix="Install QEMU 6.0 or newer",
        )
    return DoctorCheck(
        name="qemu-version",
        status="pass",
        detail=version,
    )


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


class WorkerNodeSecurityError(SmolVMError):
    """Raised when one or more host-level security checks fail.

    The reconciler must refuse to start when this is raised rather than
    running in a degraded security posture (C2: Defence in Depth).
    """


def _doctorize_worker_security_check(check: DoctorCheck) -> DoctorCheck:
    """Render worker hardening failures as warnings in the default doctor report.

    Local users should be able to validate whether SmolVM can run on their
    machine without first applying every host hardening setting expected of a
    multi-tenant worker fleet. The dedicated worker startup guard remains
    strict via ``check_worker_node_security()``.
    """
    if not check.name.startswith("worker:") or check.status != "fail":
        return check

    return DoctorCheck(
        name=check.name,
        status="warn",
        detail=check.detail,
        fix=check.fix,
    )


# ---------------------------------------------------------------------------
# Worker-node security invariants (Decision 1.1.5)
# ---------------------------------------------------------------------------

_KSM_RUN = Path("/sys/kernel/mm/ksm/run")
_THP_ENABLED = Path("/sys/kernel/mm/transparent_hugepage/enabled")
_KVM_NX_PARAM = Path("/sys/module/kvm/parameters/nx_huge_pages")
_KVM_DEV = Path("/dev/kvm")
_PROC_MEMINFO = Path("/proc/meminfo")
_FSTAB = Path("/etc/fstab")


def _check_swap_disabled() -> DoctorCheck:
    """C1: swap off prevents guest memory pages (potentially containing secrets) from
    being written to the host disk."""
    name = "worker:swap-disabled"

    # 1. Is swap inactive right now?
    try:
        meminfo = _PROC_MEMINFO.read_text()
    except OSError as exc:
        return DoctorCheck(name=name, status="fail", detail=f"Cannot read /proc/meminfo: {exc}")

    match = re.search(r"^SwapTotal:\s+(\d+)", meminfo, re.MULTILINE)
    swap_total_kb = int(match.group(1)) if match else 0
    if swap_total_kb != 0:
        return DoctorCheck(
            name=name,
            status="fail",
            detail=f"Active ({swap_total_kb} kB)",
            fix="sudo swapoff -a",
        )

    # 2. Will it stay off after a reboot (/etc/fstab)?
    try:
        fstab_text = _FSTAB.read_text()
    except OSError:
        fstab_text = ""

    swap_entries = [
        line
        for line in fstab_text.splitlines()
        if line.strip() and not line.strip().startswith("#") and "swap" in line
    ]
    if swap_entries:
        return DoctorCheck(
            name=name,
            status="fail",
            detail="Swap entries found in /etc/fstab",
            fix="sudo sed -i '/\\bswap\\b/d' /etc/fstab",
        )

    return DoctorCheck(name=name, status="pass", detail="Inactive and absent from /etc/fstab")


def _check_ksm_disabled() -> DoctorCheck:
    """KSM off: prevents cross-VM memory-page timing side-channels."""
    name = "worker:ksm-disabled"
    if not _KSM_RUN.exists():
        # KSM not compiled in; that is fine — it cannot be used.
        return DoctorCheck(name=name, status="pass", detail="Not compiled in kernel")
    try:
        value = _KSM_RUN.read_text().strip()
    except OSError as exc:
        return DoctorCheck(name=name, status="fail", detail=f"Cannot read {_KSM_RUN}: {exc}")

    if value == "0":
        return DoctorCheck(name=name, status="pass", detail="Disabled")
    return DoctorCheck(
        name=name,
        status="fail",
        detail=f"Active (run={value})",
        fix="sudo sh -c 'echo 0 > /sys/kernel/mm/ksm/run'",
    )


def _check_thp_disabled() -> DoctorCheck:
    """THP=never: prevents latency spikes that could disrupt VM timing guarantees."""
    name = "worker:thp-disabled"
    if not _THP_ENABLED.exists():
        return DoctorCheck(name=name, status="pass", detail="Not compiled in kernel")
    try:
        raw = _THP_ENABLED.read_text()
    except OSError as exc:
        return DoctorCheck(name=name, status="fail", detail=f"Cannot read {_THP_ENABLED}: {exc}")

    # File content looks like: "always madvise [never]"
    bracket_match = re.search(r"\[(\w+)\]", raw)
    active = bracket_match.group(1) if bracket_match else raw.split()[0]

    if active == "never":
        return DoctorCheck(name=name, status="pass", detail="Disabled ('never')")
    return DoctorCheck(
        name=name,
        status="fail",
        detail=f"Active ('{active}')",
        fix="sudo sh -c 'echo never > /sys/kernel/mm/transparent_hugepage/enabled'",
    )


def _check_kvm_nx_huge_pages() -> DoctorCheck:
    """CVE-2021-3737 / KVM iTLB multihit: nx_huge_pages must be 'never'."""
    name = "worker:kvm-nx-huge-pages"
    if not _KVM_NX_PARAM.exists():
        return DoctorCheck(
            name=name,
            status="warn",
            detail="Module parameter absent (kvm module not loaded?)",
            fix="sudo modprobe kvm nx_huge_pages=never",
        )
    try:
        value = _KVM_NX_PARAM.read_text().strip().lower()
    except OSError as exc:
        return DoctorCheck(name=name, status="fail", detail=f"Cannot read {_KVM_NX_PARAM}: {exc}")

    # Kernel exposes this as "never" or "N" depending on version.
    if value in {"never", "n"}:
        return DoctorCheck(name=name, status="pass", detail=f"Mitigated ('{value}')")

    return DoctorCheck(
        name=name,
        status="fail",
        detail=f"nx_huge_pages='{value}'",
        fix="sudo modprobe -r kvm_intel kvm && sudo modprobe kvm nx_huge_pages=never",
    )


def _check_kvm_runtime() -> DoctorCheck:
    """Check that /dev/kvm exists and the current user can read & write it.

    Distinguishes the four states a first-time user actually hits:
    no /dev/kvm at all (no virt support), /dev/kvm present but the user is
    not in the kvm group (fresh install), /dev/kvm present and the user is
    in the kvm group on disk but the current shell session hasn't picked
    it up yet (the common "I just ran usermod and it still fails" case),
    or fully accessible.
    """
    name = "kvm"
    if not _KVM_DEV.exists():
        return DoctorCheck(
            name=name,
            status="fail",
            detail="/dev/kvm not found on this host",
            fix="Enable hardware virtualization (KVM) on this host or run on a host that has it.",
        )
    if not os.access(_KVM_DEV, os.R_OK | os.W_OK):
        if _user_is_pending_kvm_group():
            return DoctorCheck(
                name=name,
                status="fail",
                detail=(
                    "/dev/kvm exists and your user is in the kvm group, "
                    "but this shell session predates the change"
                ),
                fix=(
                    "Log out of this shell and reconnect to apply the new group, "
                    "or run any other 'smolvm' command (such as 'smolvm list') — "
                    "it will auto-activate the group via 'sg kvm' for that run."
                ),
            )
        return DoctorCheck(
            name=name,
            status="fail",
            detail="/dev/kvm exists but your user can't read or write it",
            fix=(
                "Add your user to the kvm group, then start a new login session: "
                "'sudo usermod -aG kvm $USER' and either log out and back in, "
                "or run 'newgrp kvm' in this shell."
            ),
        )
    return DoctorCheck(name=name, status="pass", detail="/dev/kvm is available")


def _user_is_pending_kvm_group() -> bool:
    """True iff the current user is listed in /etc/group's kvm but the gid
    is not effective for this process — i.e. a usermod has been run but no
    fresh login session has rebuilt the group set.

    The effective check covers two paths: the kvm gid in the supplementary
    group list (``os.getgroups()``), and the kvm gid as the process's
    primary group (``os.getegid()``) — Linux does not always include the
    primary gid in the supplementary list, so checking only one would
    falsely flag users whose primary group is kvm."""
    try:
        import grp
        import pwd

        kvm_entry = grp.getgrnam("kvm")
        current_user = pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, ImportError):
        return False
    if current_user not in kvm_entry.gr_mem:
        return False
    return kvm_entry.gr_gid not in os.getgroups() and os.getegid() != kvm_entry.gr_gid


def _check_kvm_permissions() -> DoctorCheck:
    """Firecracker (jailer) needs /dev/kvm with 660 + kvm group ownership."""
    name = "worker:kvm-permissions"
    if not _KVM_DEV.exists():
        return DoctorCheck(name=name, status="fail", detail="/dev/kvm not found")

    stat = _KVM_DEV.stat()
    # Mode bits: 0o660 means rw-rw----
    current_mode = oct(stat.st_mode & 0o777)
    import grp as _grp  # stdlib; import locally to avoid top-level overhead

    try:
        current_group = _grp.getgrgid(stat.st_gid).gr_name
    except KeyError:
        current_group = str(stat.st_gid)

    ok_perms = (stat.st_mode & 0o777) == 0o660
    ok_group = current_group == "kvm"

    if ok_perms and ok_group:
        return DoctorCheck(name=name, status="pass", detail=f"{current_mode} group={current_group}")

    problems = []
    if not ok_perms:
        problems.append(f"mode={current_mode}")
    if not ok_group:
        problems.append(f"group={current_group}")

    return DoctorCheck(
        name=name,
        status="fail",
        detail="Incorrect permissions: " + " and ".join(problems),
        fix="sudo chmod 660 /dev/kvm && sudo chgrp kvm /dev/kvm",
    )


def check_worker_node_security() -> list[DoctorCheck]:
    """Run all host-level security invariants required before starting the reconciler.

    Returns a list of :class:`DoctorCheck` results. Raises
    :class:`WorkerNodeSecurityError` if **any** check is non-passing.

    Design rationale (C2 — Defence in Depth):
    These checks operate at the host kernel level.  No amount of application
    code can compensate for a wrong setting here.  The reconciler must call
    this function at startup and abort if it raises.

    Example usage in a reconciler entrypoint::

        from smolvm.host.doctor import check_worker_node_security, WorkerNodeSecurityError

        try:
            check_worker_node_security()
        except WorkerNodeSecurityError as exc:
            logger.critical("Worker node security check failed: %s", exc)
            sys.exit(1)
    """
    checks: list[DoctorCheck] = [
        _check_swap_disabled(),
        _check_ksm_disabled(),
        _check_thp_disabled(),
        _check_kvm_nx_huge_pages(),
        _check_kvm_permissions(),
    ]

    non_passing = [c for c in checks if c.status != "pass"]
    if non_passing:
        msg_parts = []
        for c in non_passing:
            part = f"{c.name} ({c.status}): {c.detail}"
            if c.fix:
                part += f" - Fix: {c.fix}"
            msg_parts.append(part)
        lines = " | ".join(msg_parts)
        raise WorkerNodeSecurityError(
            f"Worker node security checks failed ({len(non_passing)}/{len(checks)}): {lines}"
        )

    return checks


def generate_doctor_report(backend: str | None = None) -> DoctorReport:
    """Collect diagnostics for the selected runtime backend."""
    requested = (backend or BACKEND_AUTO).strip().lower()
    resolved = resolve_backend(requested)

    checks: list[DoctorCheck] = []

    if resolved == BACKEND_FIRECRACKER:
        host = HostManager()

        checks.append(_check_kvm_runtime())

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

        # Worker-node host-level security invariants (Decision 1.1.5).
        # These are surfaced as warnings in the default doctor report so local
        # evaluation does not require full worker-node hardening. The
        # reconciler startup guard calls check_worker_node_security()
        # separately and refuses to start on any non-pass result.
        checks.extend(
            [
                _doctorize_worker_security_check(_check_swap_disabled()),
                _doctorize_worker_security_check(_check_ksm_disabled()),
                _doctorize_worker_security_check(_check_thp_disabled()),
                _doctorize_worker_security_check(_check_kvm_nx_huge_pages()),
                _doctorize_worker_security_check(_check_kvm_permissions()),
            ]
        )

    elif resolved == BACKEND_QEMU:
        qemu = _find_qemu_binary()
        if qemu is None:
            checks.append(
                DoctorCheck(
                    name="qemu",
                    status="fail",
                    detail=(
                        "QEMU not found. Install one of: qemu-system-aarch64, qemu-system-x86_64"
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

            checks.append(_check_qemu_version(qemu_path))
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

        checks.append(_check_command("qemu-img", "qemu"))
        checks.append(_check_command("ssh", "openssh-client"))
    elif resolved == BACKEND_LIBKRUN:
        checks.append(_check_command("krunvm", "krunvm/libkrun"))
        checks.append(_check_command("ssh", "openssh-client"))
        if platform.system() != "Darwin":
            checks.append(
                DoctorCheck(
                    name="libkrun-platform",
                    status="warn",
                    detail=(
                        "libkrun backend is currently tuned for macOS in SmolVM; "
                        "use with care on non-Darwin hosts"
                    ),
                )
            )

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


def _doctor_payload(report: DoctorReport, strict: bool, exit_code: int) -> dict[str, object]:
    """Build the structured doctor data payload."""
    return {
        "backend_requested": report.backend_requested,
        "backend_resolved": report.backend_resolved,
        "system": report.system,
        "arch": report.arch,
        "checks": [asdict(check) for check in report.checks],
        "summary": {
            "failures": len(report.failures),
            "warnings": len(report.warnings),
            "ok": exit_code == 0,
            "strict": strict,
        },
    }


def _print_human_report(report: DoctorReport, strict: bool, exit_code: int) -> None:
    """Render the doctor report with Rich."""
    console = console_stdout()
    failures = len(report.failures)
    warnings = len(report.warnings)
    result_text = "OK" if exit_code == 0 else "FAIL"
    summary_lines = [
        f"Backend: {report.backend_resolved}",
        f"Platform: {report.system} {report.arch}",
        f"Result: {result_text}",
        f"Failures: {failures}",
        f"Warnings: {warnings}",
    ]
    if strict and warnings and not failures and exit_code == 1:
        summary_lines.append("strict mode treats warnings as failures.")
    console.print(
        Panel.fit(
            "\n".join(summary_lines),
            title="SmolVM Doctor",
            border_style="green" if exit_code == 0 else "red",
        )
    )

    checks_table = Table(title="Checks")
    checks_table.add_column("Check")
    checks_table.add_column("Status")
    checks_table.add_column("Detail")
    checks_table.add_column("Fix")
    for check in report.checks:
        checks_table.add_row(
            check.name,
            Text(check.status, style=status_style(check.status)),
            check.detail,
            check.fix or "-",
        )
    console.print(checks_table)


def run_doctor(
    *,
    backend: str | None = None,
    json_output: bool = False,
    strict: bool = False,
) -> int:
    """Run host diagnostics and return a process-style exit code."""
    try:
        report = generate_doctor_report(backend=backend)
        failures = len(report.failures)
        warnings = len(report.warnings)
        exit_code = 1 if failures > 0 or (strict and warnings > 0) else 0
        data = _doctor_payload(report, strict, exit_code)

        if json_output:
            emit_json("doctor", exit_code, data=data)
        else:
            _print_human_report(report, strict=strict, exit_code=exit_code)

        return exit_code
    except Exception as exc:
        if json_output:
            emit_json(
                "doctor",
                1,
                data=None,
                error={"message": str(exc), "type": "runtime_error"},
            )
        else:
            render_error(f"Error: {exc}")
        return 1
