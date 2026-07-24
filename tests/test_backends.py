import contextlib
from unittest.mock import patch

import pytest

from smolvm.exceptions import SmolVMError
from smolvm.runtime import backends as b
from smolvm.runtime.backends import (
    BACKEND_FIRECRACKER,
    BACKEND_LIBKRUN,
    BACKEND_QEMU,
    BACKEND_VZ,
    ensure_backend_available,
    resolve_backend,
    resolve_backend_for_guest,
    resolve_backend_status,
)


def _env(
    *,
    system="Linux",
    fc_binary=False,
    kvm=False,
    qemu_system=False,
    qemu_img=False,
    libkrun=False,
    lume=False,
    arch="x86_64",
    mac_version="14.0",
):
    """Patch the low-level host probes so backend detection is deterministic."""
    stack = contextlib.ExitStack()
    stack.enter_context(patch("smolvm.runtime.backends.platform.system", return_value=system))
    stack.enter_context(patch("smolvm.runtime.backends.platform.machine", return_value=arch))
    stack.enter_context(
        patch(
            "smolvm.runtime.backends.platform.mac_ver", return_value=(mac_version, ("", "", ""), "")
        )
    )
    stack.enter_context(
        patch("smolvm.runtime.backends._firecracker_binary_present", return_value=fc_binary)
    )
    stack.enter_context(patch("smolvm.runtime.backends._kvm_accessible", return_value=kvm))
    stack.enter_context(
        patch(
            "smolvm.runtime.backends._qemu_system_binary",
            return_value="qemu-system-x86_64" if qemu_system else None,
        )
    )
    stack.enter_context(patch("smolvm.runtime.backends._qemu_img_present", return_value=qemu_img))
    stack.enter_context(patch("smolvm.runtime.backends.libkrun_available", return_value=libkrun))
    stack.enter_context(patch("smolvm.runtime.backends._lume_binary_present", return_value=lume))
    return stack


# --- auto selection --------------------------------------------------------


def test_resolve_backend_auto_defaults_to_qemu_on_macos() -> None:
    with _env(system="Darwin", qemu_system=True, qemu_img=True):
        assert resolve_backend("auto") == BACKEND_QEMU


def test_resolve_backend_auto_skips_firecracker_on_macos_even_if_binary_present() -> None:
    # Firecracker is never a macOS candidate; QEMU wins.
    with _env(system="Darwin", fc_binary=True, kvm=True, qemu_system=True, qemu_img=True):
        assert resolve_backend("auto") == BACKEND_QEMU


def test_resolve_backend_accepts_libkrun_explicitly() -> None:
    assert resolve_backend(BACKEND_LIBKRUN) == BACKEND_LIBKRUN


def test_guest_aware_resolver_selects_vz_only_for_macos() -> None:
    with _env(system="Darwin", arch="arm64", qemu_system=True, qemu_img=True, lume=True):
        assert resolve_backend_for_guest("auto", "macos") == BACKEND_VZ
        assert resolve_backend_for_guest("auto", "alpine") == BACKEND_QEMU


def test_macos_guest_rejects_non_vz_backend() -> None:
    with pytest.raises(ValueError, match="macOS guests require backend 'vz'"):
        resolve_backend_for_guest(BACKEND_QEMU, "macos")


def test_resolve_backend_auto_prefers_firecracker_on_linux() -> None:
    with _env(fc_binary=True, kvm=True, qemu_system=True, qemu_img=True):
        assert resolve_backend("auto") == BACKEND_FIRECRACKER


def test_resolve_backend_auto_falls_back_to_qemu_when_firecracker_missing() -> None:
    # The reported bug: /dev/kvm is present but Firecracker isn't installed, so
    # auto must pick QEMU rather than the missing preferred backend.
    with _env(fc_binary=False, kvm=True, qemu_system=True, qemu_img=True):
        assert resolve_backend("auto") == BACKEND_QEMU


def test_resolve_backend_auto_skips_firecracker_without_kvm_when_qemu_runs() -> None:
    # Firecracker binary present but /dev/kvm inaccessible: auto must not pick a
    # backend the host can't run when QEMU is fully usable.
    with _env(fc_binary=True, kvm=False, qemu_system=True, qemu_img=True):
        assert resolve_backend("auto") == BACKEND_QEMU


def test_resolve_backend_auto_falls_back_to_libkrun_when_only_libkrun_present() -> None:
    with _env(fc_binary=False, kvm=False, qemu_system=False, qemu_img=False, libkrun=True):
        assert resolve_backend("auto") == BACKEND_LIBKRUN


def test_resolve_backend_auto_defaults_to_firecracker_when_nothing_installed() -> None:
    # With nothing detected, auto keeps the platform default so the downstream
    # preflight can surface an actionable "install Firecracker" message.
    with _env():
        assert resolve_backend("auto") == BACKEND_FIRECRACKER


def test_auto_backend_does_not_reprobe_the_fallback() -> None:
    # Nothing installed: each preferred backend is probed exactly once, and the
    # platform-default fallback reuses the status probed in the first iteration
    # instead of probing it again.
    with _env(), patch("smolvm.runtime.backends._backend_status", wraps=b._backend_status) as spy:
        backend, status = b._auto_backend()
    assert backend == BACKEND_FIRECRACKER
    assert status is not None
    assert spy.call_count == len(b._AUTO_PREFERENCE["_default"])


# --- status threading (single probe) ---------------------------------------


def test_resolve_backend_status_returns_probed_status_for_auto() -> None:
    with _env(fc_binary=True, kvm=True):
        backend, status = resolve_backend_status("auto")
    assert backend == BACKEND_FIRECRACKER
    assert status is not None and status.available is True


def test_resolve_backend_status_defers_probe_for_explicit_backend() -> None:
    # Explicit backends are returned unprobed (status None); the check is
    # deferred to ensure_backend_available so callers that only need the name
    # stay cheap.
    with _env():
        backend, status = resolve_backend_status(BACKEND_QEMU)
    assert backend == BACKEND_QEMU
    assert status is None


def test_ensure_backend_available_uses_supplied_status_without_reprobing() -> None:
    from smolvm.runtime.backends import BackendStatus

    good = BackendStatus(available=True, primary_present=True, message=None)
    with patch("smolvm.runtime.backends._backend_status") as probe:
        ensure_backend_available(BACKEND_QEMU, good)
    probe.assert_not_called()


# --- preflight messages ----------------------------------------------------


def test_ensure_backend_available_passes_when_qemu_installed() -> None:
    with _env(qemu_system=True, qemu_img=True):
        ensure_backend_available(BACKEND_QEMU)  # does not raise


def test_ensure_backend_available_raises_for_missing_qemu() -> None:
    with (
        _env(qemu_system=False, qemu_img=False),
        pytest.raises(SmolVMError, match="QEMU isn't installed"),
    ):
        ensure_backend_available(BACKEND_QEMU)


def test_ensure_backend_available_qemu_img_only_missing_gives_accurate_message() -> None:
    # qemu-system present but qemu-img absent must NOT claim QEMU is uninstalled.
    with (
        _env(qemu_system=True, qemu_img=False),
        pytest.raises(SmolVMError, match="qemu-img") as excinfo,
    ):
        ensure_backend_available(BACKEND_QEMU)
    assert "isn't installed" not in str(excinfo.value)


def test_ensure_backend_available_raises_for_missing_firecracker() -> None:
    with _env(fc_binary=False), pytest.raises(SmolVMError, match="Firecracker isn't installed"):
        ensure_backend_available(BACKEND_FIRECRACKER)


def test_ensure_backend_available_firecracker_present_without_kvm_reports_kvm() -> None:
    with _env(fc_binary=True, kvm=False), pytest.raises(SmolVMError, match="/dev/kvm"):
        ensure_backend_available(BACKEND_FIRECRACKER)


def test_firecracker_missing_message_interpolates_sandbox_name() -> None:
    # The recovery command must name the sandbox with --name so it is runnable.
    with _env(fc_binary=False), pytest.raises(SmolVMError) as excinfo:
        ensure_backend_available(BACKEND_FIRECRACKER, vm_name="sbx-einstein")
    assert "smolvm sandbox create --name sbx-einstein --backend qemu" in str(excinfo.value)


def test_firecracker_kvm_message_interpolates_sandbox_name() -> None:
    with _env(fc_binary=True, kvm=False), pytest.raises(SmolVMError) as excinfo:
        ensure_backend_available(BACKEND_FIRECRACKER, vm_name="sbx-einstein")
    assert "smolvm sandbox create --name sbx-einstein --backend qemu" in str(excinfo.value)


def test_firecracker_recovery_command_omits_name_when_unknown() -> None:
    with _env(fc_binary=False), pytest.raises(SmolVMError) as excinfo:
        ensure_backend_available(BACKEND_FIRECRACKER)
    message = str(excinfo.value)
    assert "--name" not in message
    assert "smolvm sandbox create --backend qemu" in message


def test_ensure_backend_available_raises_for_missing_libkrun() -> None:
    with _env(libkrun=False), pytest.raises(SmolVMError, match="libkrun isn't installed"):
        ensure_backend_available(BACKEND_LIBKRUN)


def test_vz_backend_requires_apple_silicon_macos_and_lume() -> None:
    with (
        _env(system="Linux", arch="x86_64", lume=True),
        pytest.raises(SmolVMError, match="Apple Silicon Mac"),
    ):
        ensure_backend_available(BACKEND_VZ)
    with (
        _env(system="Darwin", arch="arm64", lume=False),
        pytest.raises(SmolVMError, match="smolvm setup --macos"),
    ):
        ensure_backend_available(BACKEND_VZ)
    with (
        _env(system="Darwin", arch="arm64", mac_version="13.6", lume=True),
        pytest.raises(SmolVMError, match="macOS 14 or newer"),
    ):
        ensure_backend_available(BACKEND_VZ)
    with _env(system="Darwin", arch="arm64", lume=True):
        ensure_backend_available(BACKEND_VZ)


def test_ensure_backend_available_rejects_unknown_backend() -> None:
    # An unrecognized backend must not silently report success.
    with pytest.raises(ValueError, match="Unsupported backend 'xen'"):
        ensure_backend_available("xen")


# --- probe details ---------------------------------------------------------


def test_qemu_available_needs_both_system_and_img() -> None:
    with _env(qemu_system=True, qemu_img=False):
        assert b.qemu_available() is False
    with _env(qemu_system=True, qemu_img=True):
        assert b.qemu_available() is True


def test_firecracker_available_needs_binary_and_kvm() -> None:
    with _env(fc_binary=True, kvm=False):
        assert b.firecracker_available() is False
    with _env(fc_binary=True, kvm=True):
        assert b.firecracker_available() is True
