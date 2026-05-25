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

"""Guest-OS-specific QEMU command fragments.

A ``GuestPlatformSpec`` is a pure-data description of the bits that differ
between guest operating systems when assembling a ``qemu-system-x86_64`` (or
``qemu-system-aarch64``) command line: machine sub-options, CPU extra flags,
firmware (OVMF) split-pflash, sidecar requirements (swtpm for Windows), root
disk controller and CD-ROM bus topology, and a small set of "skip this Linux
default" flags.

The QEMU command builder in ``smolvm.runtime.qemu_args`` reads these fields
and concatenates them into argv. There is one spec per supported guest OS:

- ``_LINUX_SPEC``: every field at its default value, so the builder produces
  byte-identical output to the pre-refactor ``_start_qemu``.
- Windows spec: built by ``_build_windows_spec`` (Phase 2 — currently raises
  ``NotImplementedError``). When present, encodes q35+SMM, OVMF Secure Boot
  pflash split, swtpm requirement, virtio-scsi root disk, IDE CD-ROMs,
  qemu-xhci+usb-tablet topology, and Hyper-V enlightenments.

This module deliberately uses a frozen ``@dataclass`` (mirroring
``BootProfileSpec`` in ``boot_profiles.py``) rather than a ``Protocol`` with
concrete subclasses — there are only two implementations, and one of them
(Linux) is the no-op default-valued instance. Polymorphism would be ceremony
without payoff.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from smolvm.types import GuestOS


@dataclass(frozen=True, slots=True)
class FirmwareSpec:
    """OVMF/EDK2 split-pflash firmware specification.

    QEMU loads UEFI firmware as a pair of pflash drives: a read-only,
    cross-VM ``code`` file (the firmware binary) plus a writable per-VM
    ``vars`` file (the UEFI non-volatile variable store — Secure Boot keys,
    boot order, BootCurrent). The ``vars_template_path`` is the system-wide
    template; each VM gets a copy materialized into its per-VM firmware
    directory at create time.
    """

    code_path: Path
    """Read-only firmware binary (``OVMF_CODE.secboot.fd`` etc.). Shared
    across every VM on the host."""

    vars_template_path: Path
    """Read-only NVRAM template (``OVMF_VARS.ms.fd`` etc.) with the
    Microsoft Secure Boot keys pre-enrolled. Copied per-VM."""

    secure_boot: bool = True
    """When True, the QEMU command line emits the ``-global`` flags that
    mark the second pflash unit as a SMM-protected "secure" pflash, so the
    guest OS cannot tamper with Secure Boot variables at runtime."""


@dataclass(frozen=True, slots=True)
class GuestPlatformSpec:
    """Static, per-guest-OS knobs consumed when assembling a QEMU command line.

    This is *data*, not behaviour. ``smolvm.runtime.qemu_args.build_qemu_argv``
    reads these fields and translates them into argv fragments. Every field
    has a Linux-compatible default so the all-default instance (``_LINUX_SPEC``)
    produces byte-identical output to the pre-refactor ``_start_qemu``.
    """

    guest_os: GuestOS
    name: Literal["linux", "windows"]

    # --- forced settings (None = leave as VMConfig says) -------------------
    forced_boot_mode: Literal["direct_kernel", "firmware"] | None = None
    """Defensive sanity check: when set, ``build_qemu_argv`` asserts the
    VMConfig's boot_mode matches. The authoritative invariant lives on
    ``VMConfig._check_boot_mode_consistency`` — this field exists only so
    a misconfigured VM is caught loudly inside the builder."""

    skip_kernel_cmdline_injection: bool = False
    """When True, ``build_qemu_argv`` skips passing ``-kernel``/``-append``/
    ``-initrd`` entirely. Used by Windows (no Linux kernel)."""

    skip_workspace_mounts: bool = False
    """When True, ``build_qemu_argv`` ignores ``config.workspace_mounts``.
    Windows has no usable 9p client today."""

    # --- machine / cpu fragments appended after the existing q35/virt base -
    machine_extra_opts: tuple[str, ...] = ()
    """Comma-appended after the base machine string. Empty for Linux;
    Windows uses ``('smm=on', 'vmport=off', 'kernel-irqchip=on')``."""

    cpu_extra_flags: tuple[str, ...] = ()
    """Comma-appended to ``-cpu host``. Empty for Linux; Windows uses the
    Hyper-V enlightenment set (``hv_relaxed,hv_vapic,...``)."""

    extra_globals: tuple[str, ...] = ()
    """Each emitted as ``-global <value>``. Empty for Linux; Windows uses
    the SMM-protected pflash and S3-disable globals."""

    extra_objects: tuple[str, ...] = ()
    """Each emitted as ``-object <value>``. Empty for Linux; Windows uses
    ``iothread,id=iothr0`` and ``memory-backend-memfd,...``."""

    extra_devices: tuple[str, ...] = ()
    """Each emitted as ``-device <value>``. Emitted in declaration order
    — declare controllers before consumers (e.g. ``qemu-xhci`` before
    ``usb-tablet,bus=xhci.0``)."""

    # --- block / disk topology ---------------------------------------------
    root_disk_controller: Literal["virtio-blk-pci", "virtio-scsi-pci"] | None = None
    """When None, the root disk is emitted as a bare ``virtio-blk-pci``
    device (the Linux default). When set, the builder emits a controller
    of this type first, then the root drive as ``root_disk_device`` bound
    to that controller."""

    root_disk_device: Literal["virtio-blk", "scsi-hd"] = "virtio-blk"
    """Drive device kind. ``scsi-hd`` is bound to a virtio-scsi controller
    declared via ``root_disk_controller``. Windows uses ``scsi-hd``."""

    cdrom_bus: Literal["ide"] | None = None
    """How ``.iso`` files in ``extra_drives`` are attached. ``None`` (the
    Linux default) keeps the existing behaviour — ISO is attached on the
    same virtio-blk bus as any other extra drive. ``"ide"`` (Windows)
    switches to ``ide-cd`` so Windows' inbox AHCI driver can read install
    media even before the ``vioscsi`` driver is loaded mid-install. Each
    AHCI port holds exactly one drive, so the builder round-robins ISO
    entries across ``ide.0..ide.5``."""

    # --- firmware + sidecars ----------------------------------------------
    firmware: FirmwareSpec | None = None
    """When set, the builder emits split-pflash drives instead of letting
    QEMU pick the default firmware. Windows always sets this; Linux
    aarch64 still uses the legacy ``-bios`` path for now."""

    requires_swtpm: bool = False
    """When True, ``QemuRuntimeAdapter.start()`` spawns a per-VM swtpm
    sidecar before QEMU and tears it down after. Windows requires this."""

    swtpm_device_model: Literal["tpm-crb", "tpm-tis"] = "tpm-crb"
    """The TPM frontend device the guest sees. ``tpm-crb`` matches what
    physical Windows 11 hardware uses; ``tpm-tis`` is the older fallback."""


_LINUX_SPEC: GuestPlatformSpec = GuestPlatformSpec(
    guest_os=GuestOS.ALPINE,
    name="linux",
)
"""All-defaults spec for Linux guests (Alpine, Ubuntu). Produces byte-
identical QEMU argv to the pre-refactor ``_start_qemu`` when fed through
``build_qemu_argv``. Used for ``GuestOS.ALPINE`` and ``GuestOS.UBUNTU``."""


# Distro-by-distro CODE+VARS pairs for x86_64 OVMF Secure Boot firmware.
# Probed in order; the first existing pair wins. The CODE file is the
# read-only firmware binary built with SECURE_BOOT_ENABLE + SMM_REQUIRE;
# the VARS file is the NVRAM template with Microsoft Secure Boot keys
# pre-enrolled (so Windows boot manager passes Secure Boot verification).
#
# Sources for paths: docs/deep-dive/windows-guest-qemu.md §4 ("Firmware:
# split OVMF + Microsoft-keys VARS + SMM") — distro install paths table.
_X86_64_OVMF_CANDIDATE_PAIRS: tuple[tuple[str, str], ...] = (
    # Debian / Ubuntu — ovmf package, 4 MiB Secure Boot build
    (
        "/usr/share/OVMF/OVMF_CODE_4M.secboot.fd",
        "/usr/share/OVMF/OVMF_VARS_4M.ms.fd",
    ),
    # Debian / Ubuntu — legacy 2 MiB build (older releases)
    (
        "/usr/share/OVMF/OVMF_CODE.secboot.fd",
        "/usr/share/OVMF/OVMF_VARS.ms.fd",
    ),
    # Fedora / RHEL — edk2-ovmf package
    (
        "/usr/share/edk2/ovmf/OVMF_CODE.secboot.fd",
        "/usr/share/edk2/ovmf/OVMF_VARS.secboot.fd",
    ),
    (
        "/usr/share/edk2-ovmf/OVMF_CODE.secboot.fd",
        "/usr/share/edk2-ovmf/OVMF_VARS.secboot.fd",
    ),
    # Arch Linux — edk2-ovmf package, 4 MiB Secure Boot build
    (
        "/usr/share/edk2/x64/OVMF_CODE.secboot.4m.fd",
        "/usr/share/edk2/x64/OVMF_VARS.4m.fd",
    ),
    # macOS Homebrew — bundled with qemu (Apple Silicon)
    (
        "/opt/homebrew/share/qemu/edk2-x86_64-code.fd",
        "/opt/homebrew/share/qemu/edk2-i386-vars.fd",
    ),
    # macOS Homebrew — Intel
    (
        "/usr/local/share/qemu/edk2-x86_64-code.fd",
        "/usr/local/share/qemu/edk2-i386-vars.fd",
    ),
)


def _find_x86_64_ovmf() -> FirmwareSpec | None:
    """Locate a matching x86_64 OVMF code+vars pair on the host.

    Probes the distro-by-distro candidate pairs in order; first complete
    pair (both files exist) wins. Returns ``None`` if no Secure Boot
    OVMF is present — caller emits a plain-English install hint.

    Pairs are matched by distro, so we don't accidentally combine a
    Debian CODE with a Fedora VARS template and end up with a Secure
    Boot variable store from the wrong NVRAM layout.
    """
    for code_path_str, vars_path_str in _X86_64_OVMF_CANDIDATE_PAIRS:
        code_path = Path(code_path_str)
        vars_path = Path(vars_path_str)
        if code_path.is_file() and vars_path.is_file():
            return FirmwareSpec(
                code_path=code_path,
                vars_template_path=vars_path,
                secure_boot=True,
            )
    return None


def _x86_64_ovmf_install_hint() -> str:
    """Return a plain-English install hint when no x86_64 OVMF is found."""
    return (
        "Install OVMF Secure Boot firmware to boot Windows guests "
        "(Debian/Ubuntu: `sudo apt-get install -y ovmf`; Fedora/RHEL: "
        "`sudo dnf install -y edk2-ovmf`; Arch: `sudo pacman -S --needed "
        "edk2-ovmf`; macOS: `brew reinstall qemu`)."
    )


# Hyper-V "enlightenments" — paravirtualised guest interfaces Windows
# enables when it detects a Hyper-V-compatible hypervisor. KVM exposes
# these via the CPUID leaves Windows looks at, so a properly-flagged
# QEMU/KVM guest takes Windows' fast in-guest paths (synthetic timers,
# paravirt spinlocks, paravirt IPI/TLB-flush, MSR-based clocksource)
# instead of trapping for emulated x86 hardware.
#
# This is the convergent baseline across QEMU upstream, libvirt's
# <hyperv mode='passthrough'/>, Proxmox's Windows-OS-type default, and
# Red Hat's RHEL Windows-guest guide. See docs/deep-dive/windows-guest-
# qemu.md §2 for the per-flag rationale.
_WINDOWS_HV_FLAGS: tuple[str, ...] = (
    "hv_relaxed",
    "hv_vapic",
    "hv_spinlocks=0x1fff",
    "hv_vpindex",
    "hv_runtime",
    "hv_time",
    "hv_synic",
    "hv_stimer",
    "hv_frequencies",
    "hv_tlbflush",
    "hv_ipi",
    "hv_reset",
    "+kvm_pv_eoi",
    "+kvm_pv_unhalt",
)

# q35 machine sub-options Windows 11 needs:
#  - smm=on            SMM emulation; required for OVMF Secure Boot to
#                      protect NVRAM writes from in-guest tampering.
#  - vmport=off        Disables the VMware backdoor I/O port. Windows
#                      has no driver for it; dropping it reduces attack
#                      surface (default 'auto' enables it under KVM).
#  - kernel-irqchip=on KVM handles APIC/IOAPIC in-kernel — the fast path.
_WINDOWS_MACHINE_OPTS: tuple[str, ...] = (
    "smm=on",
    "vmport=off",
    "kernel-irqchip=on",
)

# -global flags required when SMM owns the pflash NVRAM. Without these
# the guest OS could forge Secure Boot variable writes at runtime.
_WINDOWS_SMM_GLOBALS: tuple[str, ...] = (
    "driver=cfi.pflash01,property=secure,value=on",
    "ICH9-LPC.disable_s3=1",
)


def _build_windows_spec(*, host_system: str, arch: str) -> GuestPlatformSpec:
    """Build the Windows ``GuestPlatformSpec`` for this host.

    Combines:
      - The Hyper-V enlightenments baseline.
      - q35 SMM/vmport/kernel-irqchip machine sub-options.
      - SMM-protected pflash + S3-disable globals.
      - virtio-scsi root disk with scsi-hd device (Windows ``vioscsi`` driver).
      - IDE/AHCI bus for ``.iso`` extras so Windows' inbox AHCI driver can
        always read install media, regardless of whether ``vioscsi`` has
        been loaded mid-install.
      - qemu-xhci + usb-tablet (q35 ships no default USB controller).
      - swtpm + tpm-crb sidecar requirement.
      - OVMF Secure Boot firmware (split CODE/VARS).

    Raises:
        NotImplementedError: When the host is non-Linux (macOS host +
            Windows guest requires HVF for x86_64, which doesn't exist —
            see docs/deep-dive/windows-guest-qemu.md §5 "macOS caveat").
        NotImplementedError: When the host arch isn't x86_64 — Windows
            ARM64 under QEMU on Apple Silicon is theoretically possible
            but out of Phase 1 scope.
        ValueError: When no OVMF Secure Boot firmware is found on the host.
    """
    if host_system != "Linux":
        raise NotImplementedError(
            "Windows guests are only supported on Linux hosts in this release. "
            "macOS host + Windows guest needs HVF for x86_64 (doesn't exist), "
            "or Windows-on-ARM under HVF (out of scope). "
            "See docs/deep-dive/windows-guest-qemu.md §5."
        )
    arch_lower = arch.lower()
    if arch_lower not in {"x86_64", "amd64"}:
        raise NotImplementedError(
            f"Windows guests on {arch} are not supported. "
            "Phase 1 targets x86_64 hosts only."
        )

    firmware = _find_x86_64_ovmf()
    if firmware is None:
        raise ValueError(_x86_64_ovmf_install_hint())

    return GuestPlatformSpec(
        guest_os=GuestOS.WINDOWS,
        name="windows",
        forced_boot_mode="firmware",
        skip_kernel_cmdline_injection=True,
        skip_workspace_mounts=True,
        machine_extra_opts=_WINDOWS_MACHINE_OPTS,
        cpu_extra_flags=_WINDOWS_HV_FLAGS,
        extra_globals=_WINDOWS_SMM_GLOBALS,
        extra_objects=(),  # iothread/memfd deferred to Phase 2 perf work
        extra_devices=(
            "qemu-xhci,id=xhci",
            "usb-tablet,bus=xhci.0",
        ),
        root_disk_controller="virtio-scsi-pci",
        root_disk_device="scsi-hd",
        cdrom_bus="ide",
        firmware=firmware,
        requires_swtpm=True,
        swtpm_device_model="tpm-crb",
    )


def get_guest_platform(
    guest_os: GuestOS,
    *,
    host_system: str,
    arch: str,
) -> GuestPlatformSpec:
    """Return the platform spec for *guest_os*, resolving host-dependent paths.

    Linux guests (Alpine, Ubuntu) get the all-defaults ``_LINUX_SPEC``.
    Windows guests get a spec built from ``_build_windows_spec``, which
    probes the host for OVMF firmware paths.
    """
    if guest_os is GuestOS.WINDOWS:
        return _build_windows_spec(host_system=host_system, arch=arch)
    return _LINUX_SPEC


# Suppress unused-import warning for `field` when this module is later extended
# with mutable defaults via dataclasses.field(default_factory=...).
_ = field
