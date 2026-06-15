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

"""Build a Windows qcow2 image from a stock Windows ISO, unattended.

End-to-end flow driven by :class:`WindowsImageBuilder`:

1. Render :file:`autounattend.xml.tmpl` with the chosen credentials.
2. Wrap that XML into a tiny FAT-formatted ISO (volume label
   ``AUTOUNATTEND``) — Windows Setup auto-discovers any attached
   removable media holding that file.
3. Create an empty target qcow2 to install into.
4. Spawn QEMU with: Win11 ISO + virtio-win ISO + autounattend ISO +
   the empty target — all the firmware / TPM / virtio plumbing comes
   from the existing :class:`smolvm.runtime.guest_platforms.GuestPlatformSpec`
   for Windows.
5. Poll over SSH for ``C:\\smolvm-ready.txt`` — the marker the answer
   file writes at the end of FirstLogonCommands.
6. Cleanly shut Windows down and tear down the build VM. The target
   qcow2 is left behind as the build artifact.

This is a **build** operation, not a sandbox lifecycle: the resulting
qcow2 is meant to be re-used by ``SmolVM(os="windows", image=...)``,
which creates per-VM overlays on top of it (see Phase 3a).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from smolvm.exceptions import SmolVMError
from smolvm.runtime.backends import BACKEND_QEMU
from smolvm.types import GuestOS, VMConfig

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Resource lookup: the answer-file template ships beside this module.
_TEMPLATE_PATH = Path(__file__).parent / "autounattend.xml.tmpl"

# Replacement tokens used in the template. Chosen to never appear in
# real PowerShell or XML (the @@ prefix is uncommon, the SMOLVM_*
# names are unambiguous).
_TEMPLATE_TOKENS: dict[str, str] = {
    "USERNAME": "@@SMOLVM_USERNAME@@",
    "PASSWORD": "@@SMOLVM_PASSWORD@@",
    "HOSTNAME": "@@SMOLVM_HOSTNAME@@",
    "EDITION": "@@SMOLVM_EDITION@@",
}

# When this file exists in the guest, FirstLogonCommands has finished
# and the image is ready to be reused by SmolVM(os="windows", image=...).
_READY_MARKER_GUEST_PATH = r"C:\smolvm-ready.txt"

# Volume label Windows Setup auto-discovers an answer file under.
# Microsoft documents that Setup probes any attached removable media
# named AUTOUNATTEND for an `autounattend.xml` at the root.
_AUTOUNATTEND_VOLUME_LABEL = "AUTOUNATTEND"

# Default target disk size — enough room for Windows + virtio-win-tools
# + OpenSSH + headroom for the user's own software to layer on top via
# overlays. The qcow2 grows on demand; this is a virtual ceiling.
_DEFAULT_DISK_SIZE_MIB = 64 * 1024  # 64 GiB

_DEFAULT_USERNAME = "smolvm"
_DEFAULT_PASSWORD = "smolvm"  # POC default; document loudly that users should override # noqa: S105
_DEFAULT_HOSTNAME = "smolvm-win"
_DEFAULT_EDITION = "Windows 11 Pro"

# How often to poll the guest for the ready marker, and how long to
# wait overall. Unattended install of a stock Win11 ISO with our answer
# file finishes in ~20 minutes on a modern host; 45 min is a comfortable
# ceiling that catches stalls without false-positive timeouts.
_MARKER_POLL_INTERVAL_S = 30.0
_DEFAULT_BUILD_TIMEOUT_S = 45 * 60


def render_autounattend(
    *,
    username: str = _DEFAULT_USERNAME,
    password: str = _DEFAULT_PASSWORD,
    hostname: str = _DEFAULT_HOSTNAME,
    edition: str = _DEFAULT_EDITION,
) -> str:
    """Render the autounattend.xml template with the chosen values.

    Uses ``str.replace`` (not ``str.format``) so the PowerShell brace
    literals inside ``FirstLogonCommands`` don't need to be escaped in
    the template. Raises ``ValueError`` if any token survives after the
    substitution pass — surfaces typos in the template at build time
    rather than letting Setup hit a malformed answer file.
    """
    text = _TEMPLATE_PATH.read_text(encoding="utf-8")
    text = text.replace(_TEMPLATE_TOKENS["USERNAME"], username)
    text = text.replace(_TEMPLATE_TOKENS["PASSWORD"], password)
    text = text.replace(_TEMPLATE_TOKENS["HOSTNAME"], hostname)
    text = text.replace(_TEMPLATE_TOKENS["EDITION"], edition)
    if "@@SMOLVM_" in text:
        raise ValueError(
            "autounattend template still contains @@SMOLVM_* tokens after "
            "substitution — template and render_autounattend are out of sync."
        )
    return text


def build_autounattend_iso(answer_xml: str, output_iso: Path) -> Path:
    """Wrap *answer_xml* in a tiny ISO9660 image labeled AUTOUNATTEND.

    Uses ``xorrisofs`` (the modern replacement for ``genisoimage`` on
    current Debian/Ubuntu; ships with ``xorriso``). Raises
    :class:`SmolVMError` with an install hint when the binary is missing.
    The ISO ends up ~400 KB and is short-lived: it's discarded after the
    build VM stops.
    """
    xorrisofs = shutil.which("xorrisofs")
    if xorrisofs is None:
        raise SmolVMError(
            "xorrisofs is required to build the autounattend answer-file ISO. "
            "Install with 'sudo apt-get install -y xorriso' (Debian/Ubuntu), "
            "'sudo dnf install -y xorriso' (Fedora/RHEL), or 'brew install xorriso' "
            "(macOS).",
        )

    output_iso.parent.mkdir(parents=True, exist_ok=True)

    # Staging directory: xorrisofs builds the ISO from a directory.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="smolvm-autounattend-") as staging:
        staging_path = Path(staging)
        (staging_path / "autounattend.xml").write_text(answer_xml, encoding="utf-8")
        result = subprocess.run(
            [
                xorrisofs,
                "-quiet",
                "-V",
                _AUTOUNATTEND_VOLUME_LABEL,
                "-o",
                str(output_iso),
                str(staging_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise SmolVMError(
                "xorrisofs failed while building the autounattend ISO",
                {"stderr": result.stderr.strip()},
            )
    logger.info("Wrote autounattend ISO: %s (%d bytes)", output_iso, output_iso.stat().st_size)
    return output_iso


def _create_empty_qcow2(target: Path, size_mib: int) -> None:
    """Create an empty qcow2 disk of *size_mib* megabytes at *target*.

    The disk grows on demand from near-zero up to ``size_mib`` MiB as
    Windows writes to it. ``preallocation=falloc`` avoids first-write
    allocation stalls during the install without paying the time cost
    of zero-filling the whole file.
    """
    qemu_img = shutil.which("qemu-img")
    if qemu_img is None:
        raise SmolVMError(
            "qemu-img is required to create the install target disk. "
            "Install QEMU (it ships with qemu-img).",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            qemu_img,
            "create",
            "-f",
            "qcow2",
            "-o",
            "preallocation=falloc,cluster_size=64K",
            str(target),
            f"{size_mib}M",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SmolVMError(
            "qemu-img create failed while preparing the install target disk",
            {"target": str(target), "stderr": result.stderr.strip()},
        )
    logger.info("Created empty target disk: %s (%d MiB virtual)", target, size_mib)


class WindowsImageBuilder:
    """Drive an unattended Windows install end-to-end.

    Args:
        windows_iso: Path to the stock Windows ISO (download from
            https://www.microsoft.com/software-download/windows11).
        virtio_win_iso: Path to the virtio-win driver ISO (download
            from https://fedorapeople.org/groups/virt/virtio-win/
            direct-downloads/stable-virtio/virtio-win.iso).
        output_qcow2: Where to write the built image.
        username: Local admin account name baked into the install.
        password: Local admin account password baked into the install.
            **Override this in production**; the default is for POC use.
        hostname: Windows computer name.
        edition: Edition name to install, as it appears in install.wim
            (default ``"Windows 11 Pro"``).
        disk_size_mib: Virtual size of the target qcow2.
        build_timeout_s: Maximum seconds to wait for the install to
            complete. The default (~45 min) is generous; typical
            installs finish in 15-20 min.
    """

    def __init__(
        self,
        *,
        windows_iso: Path,
        virtio_win_iso: Path,
        output_qcow2: Path,
        username: str = _DEFAULT_USERNAME,
        password: str = _DEFAULT_PASSWORD,
        hostname: str = _DEFAULT_HOSTNAME,
        edition: str = _DEFAULT_EDITION,
        disk_size_mib: int = _DEFAULT_DISK_SIZE_MIB,
        build_timeout_s: float = _DEFAULT_BUILD_TIMEOUT_S,
    ) -> None:
        self.windows_iso = Path(windows_iso).expanduser().resolve()
        self.virtio_win_iso = Path(virtio_win_iso).expanduser().resolve()
        self.output_qcow2 = Path(output_qcow2).expanduser().resolve()
        self.username = username
        self.password = password
        self.hostname = hostname
        self.edition = edition
        self.disk_size_mib = disk_size_mib
        self.build_timeout_s = build_timeout_s

    def build(self) -> Path:
        """Run the unattended install. Returns the path of the built qcow2.

        Side effects:
          - Creates ``output_qcow2`` if missing (refuses to overwrite an
            existing non-empty file — explicit safeguard against
            clobbering a previously-built image).
          - Writes a small autounattend ISO under
            ``output_qcow2.parent / "autounattend-{uuid}.iso"`` and
            removes it after the build.
          - Materializes the install VM under SmolVM's per-VM state dir
            (firmware/swtpm), then deletes it on clean teardown.
        """
        self._validate_inputs()

        # Deferred imports to avoid circulars (build_image references
        # SmolVM facade, which references the runtime stack).
        from smolvm import SmolVM
        from smolvm.types import VMState

        answer_xml = render_autounattend(
            username=self.username,
            password=self.password,
            hostname=self.hostname,
            edition=self.edition,
        )

        build_id = f"buildwin-{uuid.uuid4().hex[:8]}"
        autounattend_iso = self.output_qcow2.parent / f"autounattend-{build_id}.iso"
        try:
            build_autounattend_iso(answer_xml, autounattend_iso)
            _create_empty_qcow2(self.output_qcow2, self.disk_size_mib)

            # Build the install VMConfig directly — we want disk_mode=
            # "shared" because the install IS the write workload; the
            # output qcow2 should accumulate Windows writes during the
            # build, not be cloned-then-discarded as an overlay.
            install_config = VMConfig(
                vm_id=build_id,
                rootfs_path=self.output_qcow2,
                kernel_path=None,
                backend=BACKEND_QEMU,
                guest_os=GuestOS.WINDOWS,
                boot_mode="firmware",
                disk_mode="shared",
                ssh_capable=True,
                memory=4096,
                vcpu_count=4,
                extra_drives=[
                    self.windows_iso,
                    self.virtio_win_iso,
                    autounattend_iso,
                ],
            )

            vm = SmolVM(
                install_config,
                ssh_user=self.username,
                ssh_password=self.password,
            )
            logger.info(
                "Spawning unattended-install VM %s "
                "(this takes ~15-30 minutes; periodic progress will log)",
                build_id,
            )
            try:
                vm.start(boot_timeout=300)
                self._poll_for_ready_marker(vm)
                logger.info("Build complete: shutting Windows down cleanly via SSH")
                # Trigger a clean Windows shutdown so the qcow2 isn't
                # left in a dirty state. shutdown.exe returns before the
                # OS actually halts; QEMU notices the powerdown when
                # the guest's ACPI shutdown path fires. Best-effort —
                # vm.stop() below force-stops if this fails.
                with _suppress():
                    vm.run("shutdown /s /t 0", timeout=30)
                # Give Windows a few seconds to finish ACPI shutdown
                # before we kill the QEMU process.
                time.sleep(10)
            finally:
                with _suppress():
                    vm.stop()
                # Tear down the per-VM state (firmware, swtpm, vm record)
                # but PRESERVE the output qcow2 — that's our build product.
                try:
                    if vm._info.status != VMState.RUNNING:
                        # `delete()` removes the per-VM disks/firmware
                        # dir; we have to retain the output qcow2 since
                        # in shared-disk mode it's not under disks/.
                        # SmolVMManager.delete won't touch shared rootfs.
                        vm._sdk.delete(build_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Build cleanup hit a non-fatal error: %s", exc)
        finally:
            with _suppress():
                autounattend_iso.unlink()

        logger.info("Windows image built: %s", self.output_qcow2)
        return self.output_qcow2

    def _validate_inputs(self) -> None:
        if not self.windows_iso.is_file():
            raise ValueError(
                f"Windows ISO not found at {self.windows_iso}. "
                "Download Windows 11 from "
                "https://www.microsoft.com/software-download/windows11, then save "
                f"it to that path, e.g.:\n"
                f"  curl -L -o {self.windows_iso} "
                "'<paste the direct ISO URL from the Microsoft page here>'"
            )
        if not self.virtio_win_iso.is_file():
            raise ValueError(
                f"virtio-win ISO not found at {self.virtio_win_iso}. Fetch it with:\n"
                f"  curl -L -o {self.virtio_win_iso} "
                "https://fedorapeople.org/groups/virt/virtio-win/"
                "direct-downloads/stable-virtio/virtio-win.iso"
            )
        if self.output_qcow2.exists() and self.output_qcow2.stat().st_size > 0:
            raise ValueError(
                f"Output path {self.output_qcow2} already exists and is non-empty; "
                "refusing to clobber a previously-built image. Either remove it:\n"
                f"  rm -f {self.output_qcow2}\n"
                "or move it aside:\n"
                f"  mv {self.output_qcow2} {self.output_qcow2}.bak\n"
                "or pass a different --output path."
            )

    def _poll_for_ready_marker(self, vm) -> None:  # noqa: ANN001 — circular SmolVM type
        """Block until ``C:\\smolvm-ready.txt`` is present in the guest.

        The install reboots Windows multiple times before reaching
        FirstLogonCommands (Setup → Specialize → OOBE → first login).
        SSH may be intermittently unavailable across reboots; we
        tolerate transient SSH failures and just keep polling until the
        marker appears or the build_timeout expires.
        """
        from smolvm.exceptions import SmolVMError as _Err

        deadline = time.monotonic() + self.build_timeout_s
        last_log = 0.0
        while time.monotonic() < deadline:
            try:
                # First wait for SSH to be reachable; on early polls
                # Windows is still in Setup PE so SSH is absent. We give
                # each per-poll wait a short budget so the outer loop
                # advances quickly.
                vm.wait_for_ssh(timeout=_MARKER_POLL_INTERVAL_S)
                result = vm.run(
                    f"if (Test-Path '{_READY_MARKER_GUEST_PATH}') "
                    f"{{ 'READY' }} else {{ 'NOT_YET' }}",
                    timeout=15,
                )
                if result.exit_code == 0 and "READY" in result.stdout:
                    elapsed = self.build_timeout_s - (deadline - time.monotonic())
                    logger.info(
                        "Ready marker found after %.0fs; install complete.",
                        elapsed,
                    )
                    return
            except (_Err, Exception) as exc:  # noqa: BLE001
                # Transient SSH/auth failures across reboots are expected
                # during install. Don't spam logs; emit a heartbeat every
                # ~2 minutes so the user knows we're still alive.
                now = time.monotonic()
                if now - last_log > 120:
                    logger.info(
                        "Install still in progress (waiting for guest SSH + "
                        "ready marker; last probe error: %s)",
                        type(exc).__name__,
                    )
                    last_log = now
            time.sleep(_MARKER_POLL_INTERVAL_S)

        raise SmolVMError(
            "Timed out waiting for the unattended Windows install to finish. "
            "The marker file C:\\smolvm-ready.txt never appeared in the guest "
            f"within {self.build_timeout_s:.0f}s. Inspect the partially-built "
            f"image at {self.output_qcow2} via QEMU directly to diagnose.",
        )


def _suppress():
    """Tiny contextlib.suppress(Exception) used to keep teardown best-effort."""
    from contextlib import suppress

    return suppress(Exception)
