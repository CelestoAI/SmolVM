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

"""Pure command-line assembly for the QEMU backend.

Splits the QEMU argv builder out of ``SmolVMManager._start_qemu`` so it can
be unit-tested without spawning a real ``qemu-system-x86_64`` process. The
builder is a pure function: it reads ``VMInfo`` plus a ``GuestPlatformSpec``
and returns a ``list[str]`` argv. All side effects (spawning the process,
opening log files, tracking PIDs) stay in ``_start_qemu``.

For the Linux all-defaults spec (``_LINUX_SPEC``) this function produces
output byte-identical to the pre-refactor ``_start_qemu``. The
``GuestPlatformSpec`` fields exist to let later commits add Windows-specific
fragments without disturbing the Linux path.
"""

from __future__ import annotations

import platform
from pathlib import Path

from smolvm.exceptions import SmolVMError
from smolvm.runtime.guest_platforms import GuestPlatformSpec
from smolvm.runtime.qemu import QEMU_ROOT_NODE_NAME
from smolvm.types import VMInfo

# DNS server announced to the guest by QEMU's SLIRP stack. The default would
# also work; we set it explicitly so the guest sees the same address whether
# QEMU's compiled-in default ever changes upstream.
QEMU_SLIRP_DNS = "10.0.2.3"

# Number of AHCI/SATA ports the q35 ICH9 chipset exposes. ISO extras with
# cdrom_bus="ide" occupy ide.0..ide.5 in order; any beyond port 5 fall back
# to virtio-blk-pci so the QEMU command line stays valid.
_Q35_AHCI_PORTS = 6


# Candidate UEFI firmware locations for aarch64 QEMU firmware-boot.
# Searched in order; the first existing file wins. macOS Homebrew ships
# edk2-aarch64-code.fd under the qemu data dir; Debian/Ubuntu split it into
# a separate qemu-efi-aarch64 package; RHEL uses AAVMF.
_AARCH64_EDK2_FIRMWARE_CANDIDATES: tuple[str, ...] = (
    "/opt/homebrew/share/qemu/edk2-aarch64-code.fd",
    "/usr/local/share/qemu/edk2-aarch64-code.fd",
    "/usr/share/qemu-efi-aarch64/QEMU_EFI.fd",
    "/usr/share/AAVMF/AAVMF_CODE.fd",
    "/usr/share/edk2/aarch64/QEMU_EFI.fd",
    "/usr/share/edk2-armvirt/aarch64/QEMU_EFI.fd",
)


def _find_aarch64_uefi_firmware() -> Path | None:
    """Return the first existing aarch64 UEFI firmware file, or ``None``."""
    for candidate in _AARCH64_EDK2_FIRMWARE_CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            return path
    return None


def build_qemu_argv(
    vm_info: VMInfo,
    *,
    qemu_bin: Path,
    boot_args: str,
    platform_spec: GuestPlatformSpec,
    control_socket_path: Path | None = None,
    firmware_vars_path: Path | None = None,
    swtpm_socket: Path | None = None,
    start_paused: bool = False,
    root_node_name: str = QEMU_ROOT_NODE_NAME,
    host_system: str | None = None,
) -> list[str]:
    """Build the ``qemu-system-*`` argv for *vm_info*.

    Args:
        vm_info: VM info with persisted configuration/network.
        qemu_bin: Resolved path to the QEMU binary (caller looks this up
            via ``_find_qemu_binary``). Its filename selects the arch path
            (``qemu-system-aarch64`` vs ``qemu-system-x86_64``).
        boot_args: Pre-resolved kernel command line (caller passes the
            output of ``_resolve_boot_args``).
        platform_spec: Per-guest-OS overrides. For Linux guests this is
            ``_LINUX_SPEC`` and every field is at its default — the
            returned argv is byte-identical to the pre-refactor builder.
        control_socket_path: Optional QMP control socket path. When set,
            ``-qmp unix:...,server=on,wait=off`` is appended.
        firmware_vars_path: Per-VM OVMF NVRAM file path. Required when
            ``platform_spec.firmware`` is set (Windows). The manager
            materializes this file from
            ``platform_spec.firmware.vars_template_path`` at create time
            and the runtime adapter passes the per-VM path here.
        swtpm_socket: Path to the per-VM swtpm Unix socket. Required when
            ``platform_spec.requires_swtpm`` is True (Windows). The
            adapter spawns swtpm before QEMU and passes its socket here.
        start_paused: When True, append ``-S`` so QEMU boots into the
            paused state. Used for snapshot restore.
        root_node_name: QEMU block graph node name for the primary disk.
        host_system: Override of ``platform.system()`` for testing. When
            ``None`` (the default), the live host system is used.

    Returns:
        The argv list. No side effects — caller is responsible for
        ``subprocess.Popen``.

    Raises:
        SmolVMError: When the VM has no network config; when slirp mode has
            no reserved SSH host port; when firmware boot is requested on
            aarch64 and no OVMF binary is found on the host; or when the
            platform spec requires firmware/swtpm and the caller didn't
            provide a per-VM path.
    """
    # Networking mode. ``slirp`` (default) is userspace NAT with host port
    # forwards — the macOS/dev path. ``tap`` attaches the VM to a host TAP
    # device managed by NetworkManager, giving the guest a real routable IP
    # and bringing it under the same nftables NAT/isolation rules as the
    # Firecracker backend (egress masquerade, cross-sandbox drop, IMDS block).
    tap_mode = vm_info.config.qemu_network == "tap"

    if vm_info.network is None:
        raise SmolVMError("QEMU backend requires a VM network config")
    if not tap_mode and vm_info.network.ssh_host_port is None:
        raise SmolVMError(
            "QEMU slirp networking requires a reserved ssh_host_port in VM network config"
        )

    # Defensive sanity check: if the platform spec forces a specific boot
    # mode, the VMConfig must already match. The authoritative invariant
    # lives on VMConfig._check_boot_mode_consistency — this assertion
    # surfaces design-time bugs (a Windows VM somehow constructed with
    # boot_mode='direct_kernel') as a clear runtime error.
    if (
        platform_spec.forced_boot_mode is not None
        and vm_info.config.boot_mode != platform_spec.forced_boot_mode
    ):
        raise SmolVMError(
            "VM boot_mode does not match platform spec",
            {
                "guest_os": platform_spec.guest_os.value,
                "vmconfig_boot_mode": vm_info.config.boot_mode,
                "platform_spec_forced": platform_spec.forced_boot_mode,
            },
        )

    guest_mac = vm_info.network.guest_mac.lower()

    qemu_name = qemu_bin.name
    system = host_system if host_system is not None else platform.system()

    disk_format = "qcow2" if vm_info.config.rootfs_path.suffix == ".qcow2" else "raw"
    root_drive_id = f"{root_node_name}-drive"
    drive_arg = (
        f"file={vm_info.config.rootfs_path},if=none,format={disk_format},"
        f"id={root_drive_id},node-name={root_node_name}"
    )
    if tap_mode:
        # Attach to the pre-created host TAP. script=no/downscript=no: the host
        # side (link up, IP, routes, NAT) is owned by NetworkManager, not QEMU.
        netdev_arg = (
            f"tap,id=net0,ifname={vm_info.network.tap_device},script=no,downscript=no"
        )
    else:
        hostfwd_rules = [f"hostfwd=tcp:127.0.0.1:{vm_info.network.ssh_host_port}-:22"]
        for forward in vm_info.config.port_forwards:
            hostfwd_rules.append(
                f"hostfwd=tcp:{forward.host_address}:{forward.host_port}-:{forward.guest_port}"
            )
        netdev_arg = f"user,id=net0,dns={QEMU_SLIRP_DNS},{','.join(hostfwd_rules)}"

    cmd: list[str] = [
        str(qemu_bin),
        "-smp",
        str(vm_info.config.vcpu_count),
        "-m",
        str(vm_info.config.memory),
    ]
    # Boot mode: direct-kernel passes -kernel/-append (optionally -initrd);
    # firmware mode lets QEMU boot the rootfs disk via default firmware
    # (OVMF on aarch64, SeaBIOS on x86_64) — the guest kernel lives inside
    # the rootfs image.
    if vm_info.config.boot_mode == "direct_kernel":
        cmd.extend(
            [
                "-kernel",
                str(vm_info.config.kernel_path),
                "-append",
                boot_args,
            ]
        )
    cmd.extend(
        [
            "-drive",
            drive_arg,
            "-netdev",
            netdev_arg,
            "-nographic",
            "-no-reboot",
        ]
    )
    if vm_info.config.boot_mode == "direct_kernel" and vm_info.config.initrd_path is not None:
        cmd.extend(["-initrd", str(vm_info.config.initrd_path)])

    extra_drive_ids: list[str] = []
    for index, drive_path in enumerate(vm_info.config.extra_drives):
        drive_id = f"extra{index}-drive"
        node_name = f"extra{index}"
        extra_drive_ids.append(drive_id)
        drive_suffix = drive_path.suffix.lower()
        drive_format = "qcow2" if drive_suffix == ".qcow2" else "raw"
        readonly = ["readonly=on"] if drive_suffix == ".iso" else []
        extra_drive_arg = ",".join(
            [
                f"file={drive_path}",
                "if=none",
                f"format={drive_format}",
                *readonly,
                f"id={drive_id}",
                f"node-name={node_name}",
            ]
        )
        cmd.extend(["-drive", extra_drive_arg])

    # ── virtio-9p workspace mounts ──────────────────────────────
    # Skipped entirely for guest OSes that can't mount 9p (e.g. Windows).
    workspace_fsdev_ids: list[tuple[str, str]] = []
    if not platform_spec.skip_workspace_mounts:
        for index, ws in enumerate(vm_info.config.workspace_mounts):
            tag = ws.resolved_tag(index)
            fsdev_id = f"fsdev-{tag}"
            workspace_fsdev_ids.append((fsdev_id, tag))
            fsdev_opts = f"local,id={fsdev_id},path={ws.host_path},security_model=mapped-xattr"
            if not ws.writable:
                fsdev_opts += ",readonly=on"
            cmd.extend(["-fsdev", fsdev_opts])

    if control_socket_path is not None:
        cmd.extend(
            [
                "-qmp",
                f"unix:{control_socket_path},server=on,wait=off",
            ]
        )
    if start_paused:
        cmd.append("-S")

    if "aarch64" in qemu_name:
        # Pick a hardware accelerator. Without one, QEMU falls back to
        # TCG (software emulation), which is 10-50x slower and routinely
        # blows past the 30s wait_for_ssh budget on cloud-init boots.
        # macOS → Hypervisor.framework; Linux → KVM (if /dev/kvm is
        # missing the user couldn't run firecracker either, so requiring
        # KVM here is consistent).
        if system == "Darwin":
            machine, cpu = "virt,accel=hvf", "host"
        else:
            machine, cpu = "virt,accel=kvm", "host"
        cmd.extend(["-machine", machine, "-cpu", cpu])
        if vm_info.config.boot_mode == "firmware":
            firmware_path = _find_aarch64_uefi_firmware()
            if firmware_path is None:
                raise SmolVMError(
                    "aarch64 firmware-boot requires UEFI firmware (edk2/AAVMF) "
                    "but none was found. Searched: "
                    f"{', '.join(_AARCH64_EDK2_FIRMWARE_CANDIDATES)}. "
                    "On macOS run 'brew reinstall qemu'; on Debian/Ubuntu "
                    "install 'qemu-efi-aarch64'."
                )
            cmd.extend(["-bios", str(firmware_path)])
        # virtio-MMIO device ordering note: on `-machine virt`, kernel
        # enumeration of virtio-mmio slots is the REVERSE of the order
        # the `-device` flags appear on the command line. To make sure
        # the rootdisk lands at /dev/vda (and not /dev/vdb behind the
        # cloud-init seed), the rootdisk-block device must be the LAST
        # virtio-blk-device added. Workspace fsdevs and the NIC must
        # come before it too.
        for drive_id in extra_drive_ids:
            cmd.extend(["-device", f"virtio-blk-device,drive={drive_id}"])
        for fsdev_id, tag in workspace_fsdev_ids:
            cmd.extend(["-device", f"virtio-9p-device,fsdev={fsdev_id},mount_tag={tag}"])
        cmd.extend(
            [
                "-device",
                f"virtio-net-device,netdev=net0,mac={guest_mac}",
                "-device",
                f"virtio-blk-device,drive={root_drive_id}",
            ]
        )
    else:
        # See accel comment on the aarch64 branch above. Without
        # accel=kvm on Linux, QEMU runs TCG and Ubuntu cloud-init blows
        # past wait_for_ssh in seconds vs minutes.
        if system == "Darwin":
            machine_base, cpu_base = "q35,accel=hvf", "host"
        else:
            machine_base, cpu_base = "q35,accel=kvm", "host"
        # Per-OS extras are comma-appended to the base. For _LINUX_SPEC
        # both extra tuples are empty → byte-identical to the legacy form.
        machine_str = ",".join((machine_base, *platform_spec.machine_extra_opts))
        cpu_str = ",".join((cpu_base, *platform_spec.cpu_extra_flags))
        cmd.extend(["-machine", machine_str, "-cpu", cpu_str])

        # -global / -object entries from the spec. Linux: both empty.
        for global_arg in platform_spec.extra_globals:
            cmd.extend(["-global", global_arg])
        for object_arg in platform_spec.extra_objects:
            cmd.extend(["-object", object_arg])

        # Firmware split-pflash (Windows). The CODE file is read-only and
        # shared; the VARS file is per-VM (the manager materializes it
        # from the spec's vars_template_path at create time).
        if platform_spec.firmware is not None:
            if firmware_vars_path is None:
                raise SmolVMError(
                    "Guest platform requires firmware split-pflash but no "
                    "per-VM OVMF_VARS path was provided.",
                    {"guest_os": platform_spec.guest_os.value},
                )
            cmd.extend(
                [
                    "-drive",
                    (
                        f"if=pflash,format=raw,unit=0,readonly=on,"
                        f"file={platform_spec.firmware.code_path}"
                    ),
                    "-drive",
                    f"if=pflash,format=raw,unit=1,file={firmware_vars_path}",
                ]
            )

        # vTPM (Windows). The adapter starts swtpm before QEMU and passes
        # its data-channel socket here; QEMU connects as a chardev client.
        if platform_spec.requires_swtpm:
            if swtpm_socket is None:
                raise SmolVMError(
                    "Guest platform requires swtpm but no socket path was provided.",
                    {"guest_os": platform_spec.guest_os.value},
                )
            cmd.extend(
                [
                    "-chardev",
                    f"socket,id=chrtpm,path={swtpm_socket}",
                    "-tpmdev",
                    "emulator,id=tpm0,chardev=chrtpm",
                    "-device",
                    f"{platform_spec.swtpm_device_model},tpmdev=tpm0",
                ]
            )

        # Root disk topology. None (Linux default) keeps the historical
        # virtio-blk-pci flat attachment. virtio-scsi-pci (Windows) emits
        # a SCSI controller first, then a scsi-hd device bound to it —
        # that's what the Windows vioscsi driver expects.
        if platform_spec.root_disk_controller is None:
            cmd.extend(["-device", f"virtio-blk-pci,drive={root_drive_id}"])
        else:
            cmd.extend(
                [
                    "-device",
                    f"{platform_spec.root_disk_controller},id=scsi0",
                    "-device",
                    (f"{platform_spec.root_disk_device},bus=scsi0.0,drive={root_drive_id}"),
                ]
            )

        cmd.extend(["-device", f"virtio-net-pci,netdev=net0,mac={guest_mac}"])

        # Extra drives. Each AHCI port holds exactly one drive on q35, so
        # ISO entries with cdrom_bus="ide" are placed on ide.0..ide.5 in
        # order. Once those 6 ports are exhausted, additional ISOs fall
        # back to virtio-blk-pci so the QEMU command line stays valid
        # (Windows still sees them as block devices). Non-ISO extras and
        # Linux (None cdrom_bus) keep the legacy virtio-blk-pci wiring.
        ide_port_index = 0
        for drive_id, drive_path in zip(extra_drive_ids, vm_info.config.extra_drives, strict=True):
            is_iso = drive_path.suffix.lower() == ".iso"
            use_ide = (
                is_iso and platform_spec.cdrom_bus == "ide" and ide_port_index < _Q35_AHCI_PORTS
            )
            if use_ide:
                cmd.extend(
                    [
                        "-device",
                        f"ide-cd,bus=ide.{ide_port_index},drive={drive_id}",
                    ]
                )
                ide_port_index += 1
            else:
                cmd.extend(["-device", f"virtio-blk-pci,drive={drive_id}"])

        for fsdev_id, tag in workspace_fsdev_ids:
            cmd.extend(["-device", f"virtio-9p-pci,fsdev={fsdev_id},mount_tag={tag}"])

        # Trailing per-OS -device flags from the spec (e.g. qemu-xhci then
        # usb-tablet for Windows). Emitted in declaration order so
        # controllers always come before consumers.
        for device_arg in platform_spec.extra_devices:
            cmd.extend(["-device", device_arg])

    # vsock control-plane device. The guest agent listens on this CID and the
    # host VsockChannel connects to it. Native vhost-vsock needs the host's
    # /dev/vhost-vsock, which only exists on Linux — macOS/HVF has no
    # equivalent, so we emit nothing there and the host stays on SSH. The
    # device variant differs by machine type (PCI for q35, MMIO for virt).
    if vm_info.config.vsock is not None and system == "Linux":
        vsock_device = "vhost-vsock-device" if "aarch64" in qemu_name else "vhost-vsock-pci"
        cmd.extend(["-device", f"{vsock_device},guest-cid={vm_info.config.vsock.guest_cid}"])

    return cmd
