"""Restricted QEMU spawn ordering; public starts remain gated during integration."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import smolvm.vm as module
from smolvm.network_policy import NetworkPolicy, lifecycle, placement
from smolvm.vm import SmolVMManager


@pytest.fixture
def launch(monkeypatch, tmp_path):
    events = []
    process = Mock(pid=1234)
    process.kill.side_effect = lambda: events.append("kill")
    process.wait.side_effect = lambda **kwargs: events.append("wait")
    reservation = Mock(attached=False)

    def attach(pid):
        events.append("attach")
        reservation.attached = True

    reservation.attach.side_effect = attach
    reservation.abort.side_effect = lambda: events.append("abort")
    prepare = Mock(side_effect=lambda *args: events.append("prepare") or reservation)
    monkeypatch.setattr(lifecycle, "PreparedPolicy", prepare)
    monkeypatch.setattr(placement, "validate_host", Mock())
    monkeypatch.setattr(placement, "placement", Mock(return_value=(Mock(), tmp_path / "state")))
    monkeypatch.setattr(module, "get_guest_platform", Mock())
    argv = Mock(return_value=["qemu"])
    monkeypatch.setattr(module, "build_qemu_argv", argv)
    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        Mock(side_effect=lambda *a, **k: events.append("spawn") or process),
    )
    manager = SimpleNamespace(
        data_dir=tmp_path,
        _find_qemu_binary=lambda: "qemu",
        _resolve_boot_args=lambda vm: "",
        _process_handles={},
        _log_files={},
        state=SimpleNamespace(update_vm=Mock(side_effect=lambda *a, **k: events.append("persist"))),
    )
    vm = SimpleNamespace(
        vm_id="sbx-policy",
        config=SimpleNamespace(guest_os="linux", network_policy=NetworkPolicy(allowed_domains=[])),
    )
    yield SimpleNamespace(
        manager=manager,
        vm=vm,
        process=process,
        reservation=reservation,
        prepare=prepare,
        argv=argv,
        events=events,
        path=tmp_path / "qemu.log",
    )
    for stream in manager._log_files.values():
        stream.close()


def test_spawn_is_paused_and_attached_after_process_persisted(launch):
    result = SmolVMManager._start_qemu(launch.manager, launch.vm, launch.path)
    assert result is launch.process
    assert launch.argv.call_args.kwargs["start_paused"] is True
    assert launch.events == ["prepare", "spawn", "persist", "attach"]


def test_attachment_failure_kills_and_waits_before_abort(launch):
    launch.reservation.attach.side_effect = RuntimeError("attach failed")
    with pytest.raises(RuntimeError, match="attach failed"):
        SmolVMManager._start_qemu(launch.manager, launch.vm, launch.path)
    assert launch.events == ["prepare", "spawn", "persist", "kill", "wait", "abort"]
    assert launch.manager._process_handles[1234] is launch.process


def test_state_failure_also_stops_spawned_vm(launch):
    launch.manager.state.update_vm.side_effect = RuntimeError("inventory failed")
    with pytest.raises(RuntimeError, match="inventory failed"):
        SmolVMManager._start_qemu(launch.manager, launch.vm, launch.path)
    assert launch.events == ["prepare", "spawn", "kill", "wait", "abort"]


def test_legacy_spawn_has_no_policy_side_effects(launch):
    launch.vm.config.network_policy = None
    SmolVMManager._start_qemu(launch.manager, launch.vm, launch.path)
    assert launch.argv.call_args.kwargs["start_paused"] is False
    assert launch.events == ["spawn"]
    launch.prepare.assert_not_called()
