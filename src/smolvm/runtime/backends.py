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

"""Runtime backend selection helpers for SmolVM."""

from __future__ import annotations

import logging
import os
import platform
import shlex
from pathlib import Path
from typing import NamedTuple

from smolvm.exceptions import SmolVMError
from smolvm.utils import which  # noqa: F401 — imported for test patching

logger = logging.getLogger(__name__)

BACKEND_FIRECRACKER = "firecracker"
BACKEND_QEMU = "qemu"
BACKEND_LIBKRUN = "libkrun"
BACKEND_VZ = "vz"
BACKEND_AUTO = "auto"

SUPPORTED_BACKENDS = {BACKEND_FIRECRACKER, BACKEND_QEMU, BACKEND_LIBKRUN, BACKEND_VZ}

# Where ``smolvm`` installs a private Firecracker binary when it isn't on PATH.
# Kept in sync with ``smolvm.host.manager.HostManager.BIN_DIR``; duplicated here
# so backend selection stays free of the heavier host-manager import.
_LOCAL_BIN_DIR = Path.home() / ".smolvm" / "bin"
_KVM_DEVICE = Path("/dev/kvm")


class BackendStatus(NamedTuple):
    """Result of probing one backend's host tooling.

    Attributes:
        available: The backend is fully installed and runnable here.
        primary_present: The backend's main binary/library is present even if a
            secondary requirement (KVM access, ``qemu-img``) is missing. Used to
            pick the most-relevant backend to report when none is fully runnable.
        message: Plain-English recovery when not ``available`` (else ``None``).
    """

    available: bool
    primary_present: bool
    message: str | None


# --- low-level probes (patched directly in tests) --------------------------


def _qemu_system_candidates() -> tuple[str, ...]:
    """Return host-arch-first ``qemu-system-*`` binary names to look for."""
    arch = platform.machine().lower()
    if arch in {"arm64", "aarch64"}:
        return ("qemu-system-aarch64", "qemu-system-x86_64")
    if arch in {"x86_64", "amd64"}:
        return ("qemu-system-x86_64", "qemu-system-aarch64")
    return ("qemu-system-aarch64", "qemu-system-x86_64")


def _firecracker_binary_present() -> bool:
    """Return whether a Firecracker binary is on ``PATH`` or in ``~/.smolvm/bin``."""
    if which("firecracker") is not None:
        return True
    local = _LOCAL_BIN_DIR / "firecracker"
    return local.exists() and os.access(local, os.X_OK)


def _kvm_accessible() -> bool:
    """Return whether ``/dev/kvm`` exists and the current user can read/write it."""
    return _KVM_DEVICE.exists() and os.access(_KVM_DEVICE, os.R_OK | os.W_OK)


def _qemu_system_binary() -> str | None:
    """Return the first available ``qemu-system-*`` binary name, or ``None``."""
    for candidate in _qemu_system_candidates():
        if which(candidate) is not None:
            return candidate
    return None


def _qemu_img_present() -> bool:
    """Return whether ``qemu-img`` is on ``PATH``."""
    return which("qemu-img") is not None


def libkrun_available() -> bool:
    """Return whether the libkrun shared library can be loaded."""
    try:
        from smolvm.runtime._libkrun_ffi import is_available
    except Exception:  # pragma: no cover - defensive import guard
        return False
    return is_available()


def _lume_binary_present() -> bool:
    """Return whether the exact tested preview macOS runtime is installed."""
    from smolvm.host.lume import pinned_lume_ready

    return pinned_lume_ready()


# --- per-backend status ----------------------------------------------------


def firecracker_status() -> BackendStatus:
    """Probe whether the Firecracker backend can run on this host.

    Firecracker needs both its binary and hardware virtualization
    (``/dev/kvm``); a binary without KVM access counts as installed-but-not-
    runnable so selection can prefer a backend that actually works.
    """
    if not _firecracker_binary_present():
        return BackendStatus(False, False, _firecracker_missing_message())
    if not _kvm_accessible():
        return BackendStatus(False, True, _firecracker_kvm_message())
    return BackendStatus(True, True, None)


def qemu_status() -> BackendStatus:
    """Probe whether the QEMU backend can run on this host.

    The QEMU backend needs a ``qemu-system-*`` emulator *and* ``qemu-img`` (for
    the per-VM disk overlays and snapshots). A system emulator without
    ``qemu-img`` counts as installed-but-not-runnable.
    """
    has_system = _qemu_system_binary() is not None
    has_img = _qemu_img_present()
    if has_system and has_img:
        return BackendStatus(True, True, None)
    return BackendStatus(
        False,
        has_system,
        _qemu_missing_message(has_system=has_system, has_img=has_img),
    )


def libkrun_status() -> BackendStatus:
    """Probe whether the libkrun backend can run on this host."""
    if libkrun_available():
        return BackendStatus(True, True, None)
    return BackendStatus(False, False, _libkrun_missing_message())


def vz_status() -> BackendStatus:
    """Probe the Apple Virtualization.framework macOS guest backend."""
    from smolvm.host.lume import macos_host_capabilities

    capabilities = macos_host_capabilities()
    if not capabilities.is_apple_silicon:
        return BackendStatus(False, False, _vz_unsupported_host_message())
    if not capabilities.supported_version:
        return BackendStatus(
            False,
            False,
            "macOS desktop sandboxes need macOS 14 or newer. Update this Mac, then run "
            "'smolvm doctor --backend vz' again.",
        )
    if not _lume_binary_present():
        return BackendStatus(False, False, _vz_missing_message())
    return BackendStatus(True, True, None)


def firecracker_available() -> bool:
    """Return whether the Firecracker backend is fully runnable here."""
    return firecracker_status().available


def qemu_available() -> bool:
    """Return whether the QEMU backend is fully runnable here."""
    return qemu_status().available


def _backend_status(backend: str) -> BackendStatus | None:
    """Return the availability status for a concrete backend, or ``None``."""
    if backend == BACKEND_FIRECRACKER:
        return firecracker_status()
    if backend == BACKEND_QEMU:
        return qemu_status()
    if backend == BACKEND_LIBKRUN:
        return libkrun_status()
    if backend == BACKEND_VZ:
        return vz_status()
    return None


# Auto-selection preference order per host, most-preferred first. macOS has no
# KVM, so Firecracker is not a candidate there.
_AUTO_PREFERENCE: dict[str, tuple[str, ...]] = {
    "darwin": (BACKEND_QEMU, BACKEND_LIBKRUN),
    "_default": (BACKEND_FIRECRACKER, BACKEND_QEMU, BACKEND_LIBKRUN),
}


def _auto_backend() -> tuple[str, BackendStatus | None]:
    """Pick the best backend for this host and return it with its status.

    Prefers Firecracker on Linux and QEMU on macOS, and returns the first
    backend that is fully runnable. When none is fully runnable, it returns the
    one that is *closest* to runnable (its main binary is present but a
    secondary requirement is missing) so the preflight can name the exact fix;
    failing that, the platform default. Returning the status alongside the name
    lets callers validate without probing a second time.
    """
    system = platform.system().lower()
    preference = _AUTO_PREFERENCE.get(system, _AUTO_PREFERENCE["_default"])
    partial: tuple[str, BackendStatus] | None = None
    fallback_status: BackendStatus | None = None
    for index, backend in enumerate(preference):
        status = _backend_status(backend)
        assert status is not None  # preference lists only supported backends
        if index == 0:
            # preference[0] is the platform default and the final fallback;
            # remember its status so we don't probe it a second time below.
            fallback_status = status
        if status.available:
            logger.debug("auto backend selection chose %s", backend)
            return backend, status
        if partial is None and status.primary_present:
            partial = (backend, status)
    if partial is not None:
        logger.debug(
            "auto backend selection found no fully runnable backend; "
            "reporting partially-installed %s",
            partial[0],
        )
        return partial
    fallback = preference[0]
    logger.debug("auto backend selection found no installed backend; defaulting to %s", fallback)
    return fallback, fallback_status


def resolve_backend_status(requested: str | None = None) -> tuple[str, BackendStatus | None]:
    """Resolve a backend and return it with its availability status.

    Like :func:`resolve_backend`, but also returns the probed
    :class:`BackendStatus` so a caller can call
    :func:`ensure_backend_available` without probing the host a second time.
    For an explicitly-requested backend the status is ``None`` (unprobed) — the
    check is deferred to :func:`ensure_backend_available`, keeping this cheap for
    callers that only need the name.

    Raises:
        ValueError: If the requested backend is unknown.
    """
    raw = (requested or os.environ.get("SMOLVM_BACKEND") or BACKEND_AUTO).strip().lower()

    if raw == BACKEND_AUTO:
        return _auto_backend()

    if raw in SUPPORTED_BACKENDS:
        return raw, None

    supported = ", ".join(sorted((*SUPPORTED_BACKENDS, BACKEND_AUTO)))
    raise ValueError(f"Unsupported backend '{raw}'. Supported values: {supported}")


def resolve_backend(requested: str | None = None) -> str:
    """Resolve the effective backend name.

    Resolution order:
    1) Explicit ``requested`` argument.
    2) ``SMOLVM_BACKEND`` environment variable.
    3) ``auto``: the best backend actually installed on this host
       (Firecracker preferred on Linux, QEMU on macOS), falling back to the
       next installed backend when the preferred one is missing or can't run.

    Args:
        requested: Optional backend string.

    Returns:
        Effective backend name.

    Raises:
        ValueError: If backend is unknown.
    """
    return resolve_backend_status(requested)[0]


def resolve_backend_for_guest(
    requested: str | None,
    guest_os: object,
) -> str:
    """Resolve a backend while honoring guest-specific runtime requirements.

    The legacy :func:`resolve_backend` remains host-only so existing Linux
    callers keep selecting QEMU on macOS.  macOS guests use this resolver and
    select Apple's Virtualization.framework backend instead.
    """
    guest_value = getattr(guest_os, "value", guest_os)
    if str(guest_value).lower() != "macos":
        return resolve_backend(requested)

    raw = (requested or os.environ.get("SMOLVM_BACKEND") or BACKEND_AUTO).strip().lower()
    if raw in {BACKEND_AUTO, BACKEND_VZ}:
        return BACKEND_VZ
    supported = f"{BACKEND_AUTO}, {BACKEND_VZ}"
    raise ValueError(
        f"macOS guests require backend 'vz' (got {raw!r}); supported values: {supported}"
    )


def _create_with_qemu_command(vm_name: str | None) -> str:
    """Return the exact 'create with QEMU instead' recovery command.

    Interpolates the sandbox name with ``--name`` when known so the suggested
    command is runnable as-is rather than a generic placeholder.
    """
    if vm_name:
        return f"smolvm sandbox create --name {shlex.quote(vm_name)} --backend qemu"
    return "smolvm sandbox create --backend qemu"


def _firecracker_missing_message(vm_name: str | None = None) -> str:
    """Plain-English recovery for a missing Firecracker binary."""
    return (
        "Firecracker isn't installed on this machine. Install it and make sure "
        "the 'firecracker' binary is on your PATH (or in ~/.smolvm/bin), or "
        f"create the sandbox with a different backend, e.g. '{_create_with_qemu_command(vm_name)}'."
    )


def _firecracker_kvm_message(vm_name: str | None = None) -> str:
    """Plain-English recovery for Firecracker present but KVM unusable."""
    return (
        "Firecracker needs hardware virtualization (/dev/kvm), which isn't "
        "available or accessible on this machine. Give your user access with "
        "'sudo usermod -aG kvm $USER' and start a new login session, or create "
        f"the sandbox with a different backend, e.g. '{_create_with_qemu_command(vm_name)}'."
    )


def _qemu_missing_message(*, has_system: bool, has_img: bool) -> str:
    """Plain-English recovery for missing QEMU tooling.

    Distinguishes a completely-absent QEMU from the case where the system
    emulator is present but ``qemu-img`` is missing, so the message never
    claims QEMU is uninstalled when it isn't.
    """
    if has_system and not has_img:
        return (
            "QEMU is installed but its 'qemu-img' disk tool is missing. Install "
            "it with 'sudo apt-get install -y qemu-utils' on Debian/Ubuntu, "
            "'sudo dnf install -y qemu-img' on Fedora/RHEL, or your distro's "
            "qemu-img package, then run 'smolvm doctor --backend qemu' to confirm."
        )
    system = platform.system()
    if system == "Darwin":
        install = "Install it with 'brew install qemu'"
    elif system == "Linux":
        install = (
            "Install it with 'sudo apt-get install -y qemu-system qemu-utils' on "
            "Debian/Ubuntu, 'sudo dnf install -y qemu-system-x86 qemu-img' on "
            "Fedora/RHEL, or your distro's QEMU package"
        )
    else:
        install = "Install QEMU for this operating system"
    return (
        "QEMU isn't installed on this machine. "
        f"{install}, then run 'smolvm doctor --backend qemu' to confirm."
    )


def _libkrun_missing_message() -> str:
    """Plain-English recovery for a missing libkrun library."""
    return (
        "libkrun isn't installed on this machine. Install it with "
        "'brew install libkrun/krun/libkrun' on macOS or 'sudo dnf install "
        "libkrun' on Fedora, then run 'smolvm doctor --backend libkrun' to confirm."
    )


def _vz_unsupported_host_message() -> str:
    """Recovery for trying to run a macOS guest on unsupported hardware."""
    return (
        "macOS sandboxes need an Apple Silicon Mac. Run "
        "'smolvm sandbox create --os alpine' on this machine instead."
    )


def _vz_missing_message() -> str:
    """Recovery for a supported host without the preview runtime driver."""
    return (
        "The macOS sandbox runtime isn't installed. Run 'smolvm setup --macos', then "
        "'smolvm doctor --backend vz' to confirm."
    )


def ensure_backend_available(
    backend: str,
    status: BackendStatus | None = None,
    *,
    vm_name: str | None = None,
) -> None:
    """Verify the tooling a resolved backend needs is installed.

    Call this before any slow work (such as downloading a base image) so a
    missing hypervisor fails fast with a clear, actionable message instead of
    surfacing deep inside VM start after a download.

    Args:
        backend: A resolved backend name (not ``auto``).
        status: An already-probed :class:`BackendStatus` for *backend* (e.g. from
            :func:`resolve_backend_status`). Pass it to avoid re-probing the
            host; omit it to probe now.
        vm_name: The sandbox name the caller is creating, if known. When set,
            the Firecracker recovery command names it with ``--name`` so the
            suggestion is exactly runnable.

    Raises:
        ValueError: If *backend* is not a recognized backend.
        SmolVMError: If the backend's required tooling isn't present.
    """
    if status is None:
        status = _backend_status(backend)
    if status is None:
        # An unrecognized backend has no tooling to verify; treat it as a usage
        # error rather than silently reporting the backend as available.
        supported = ", ".join(sorted(SUPPORTED_BACKENDS))
        raise ValueError(f"Unsupported backend '{backend}'. Supported values: {supported}")
    if status.available:
        return
    message = status.message
    if vm_name and backend == BACKEND_FIRECRACKER:
        # Rebuild the Firecracker recovery with the real sandbox name.
        # primary_present distinguishes a missing binary (False) from a present
        # binary without usable KVM (True), matching firecracker_status().
        message = (
            _firecracker_kvm_message(vm_name)
            if status.primary_present
            else _firecracker_missing_message(vm_name)
        )
    raise SmolVMError(message or f"Backend '{backend}' is not available.")
