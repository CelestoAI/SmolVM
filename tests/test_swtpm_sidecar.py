# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for the per-VM swtpm sidecar used by Windows guests."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.exceptions import SmolVMError
from smolvm.runtime.base import RuntimeContext
from smolvm.runtime.qemu import _SwtpmSidecar


def _make_context(firmware_dir: Path) -> RuntimeContext:
    """A minimal runtime context where only is_process_running matters."""
    return RuntimeContext(
        data_dir=Path("/tmp/data"),
        socket_dir=Path("/tmp"),
        firmware_dir=firmware_dir,
        log_files={},
        process_handles={},
        resolve_boot_args=lambda vm_info: vm_info.config.boot_args,
        start_firecracker=MagicMock(),
        start_qemu=MagicMock(),
        unlink_socket=MagicMock(),
        kill_process=MagicMock(),
        wait_for_process=MagicMock(),
        is_process_running=MagicMock(return_value=False),
        find_qemu_binary=MagicMock(),
    )


def test_socket_path_under_per_vm_state_dir(tmp_path: Path) -> None:
    """The data socket lives at firmware_dir/{vm_id}/swtpm/swtpm-sock."""
    context = _make_context(tmp_path)
    sidecar = _SwtpmSidecar(
        vm_id="vm-win",
        firmware_dir=tmp_path,
        context=context,
    )
    assert sidecar.socket_path == tmp_path / "vm-win" / "swtpm" / "swtpm-sock"
    assert sidecar.pidfile_path == tmp_path / "vm-win" / "swtpm" / "swtpm.pid"


def test_start_raises_clear_error_when_swtpm_binary_missing(tmp_path: Path) -> None:
    """Missing swtpm on PATH produces a plain-English install hint."""
    context = _make_context(tmp_path)
    sidecar = _SwtpmSidecar(
        vm_id="vm-win",
        firmware_dir=tmp_path,
        context=context,
    )
    with patch("smolvm.runtime.qemu.which", return_value=None), pytest.raises(
        SmolVMError, match="swtpm"
    ):
        sidecar.start()


def test_start_spawns_swtpm_with_expected_arguments(tmp_path: Path) -> None:
    """The swtpm invocation matches the documented daemon-socket form."""
    context = _make_context(tmp_path)
    sidecar = _SwtpmSidecar(
        vm_id="vm-win",
        firmware_dir=tmp_path,
        context=context,
    )

    fake_pid = 99001

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        # swtpm in --daemon mode forks; simulate by writing the socket + pidfile.
        sidecar.socket_path.touch()
        sidecar.pidfile_path.write_text(f"{fake_pid}\n")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    with patch(
        "smolvm.runtime.qemu.which",
        return_value=Path("/usr/bin/swtpm"),
    ), patch("smolvm.runtime.qemu.subprocess.run", side_effect=fake_run) as mock_run:
        returned_pid = sidecar.start()

    assert returned_pid == fake_pid
    cmd = mock_run.call_args.args[0]
    assert cmd[0] == "/usr/bin/swtpm"
    assert "socket" in cmd
    assert "--tpm2" in cmd
    assert "--daemon" in cmd
    # tpmstate dir, ctrl socket, pidfile all point under the per-VM state dir.
    state_dir = tmp_path / "vm-win" / "swtpm"
    assert f"dir={state_dir}" in cmd
    assert f"type=unixio,path={state_dir / 'swtpm-sock'}" in cmd
    assert f"file={state_dir / 'swtpm.pid'}" in cmd


def test_start_raises_when_socket_never_appears(tmp_path: Path) -> None:
    """If swtpm starts but never creates its socket, raise loudly."""
    context = _make_context(tmp_path)
    sidecar = _SwtpmSidecar(
        vm_id="vm-win",
        firmware_dir=tmp_path,
        context=context,
    )

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    with patch(
        "smolvm.runtime.qemu.which",
        return_value=Path("/usr/bin/swtpm"),
    ), patch("smolvm.runtime.qemu.subprocess.run", side_effect=fake_run), pytest.raises(
        SmolVMError, match="socket never appeared"
    ):
        sidecar.start(timeout=0.2)


def test_stop_sigterms_the_daemon_and_unlinks_files(tmp_path: Path) -> None:
    """stop() reads the pidfile, SIGTERMs, then removes the socket + pidfile."""
    context = _make_context(tmp_path)
    sidecar = _SwtpmSidecar(
        vm_id="vm-win",
        firmware_dir=tmp_path,
        context=context,
    )

    sidecar.socket_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar.socket_path.touch()
    sidecar.pidfile_path.write_text("12345\n")

    # First call: process is alive (signal sent). Subsequent polls: gone.
    # Extra Falses cover the post-wait re-check that would SIGKILL if needed.
    context.is_process_running.side_effect = [True, False, False, False]

    with patch("smolvm.runtime.qemu.os.kill") as mock_kill:
        sidecar.stop()

    mock_kill.assert_called_once()
    pid_arg, _signal_arg = mock_kill.call_args.args
    assert pid_arg == 12345

    assert not sidecar.socket_path.exists()
    assert not sidecar.pidfile_path.exists()


def test_stop_is_safe_when_pidfile_missing(tmp_path: Path) -> None:
    """stop() must not raise when nothing is around to clean up."""
    context = _make_context(tmp_path)
    sidecar = _SwtpmSidecar(
        vm_id="vm-win",
        firmware_dir=tmp_path,
        context=context,
    )
    # Don't create any files. stop() should be a clean no-op.
    sidecar.stop()
