"""Shutdown never signals recycled PIDs or releases uncertain placements."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from smolvm.network_policy import shutdown


@pytest.fixture
def stopped(monkeypatch, tmp_path):
    path = tmp_path / "tap2.json"
    path.write_text(json.dumps({"vm_pid": 1234, "vm_identity": "birth"}))
    vm = SimpleNamespace(vm_id="sbx-policy", pid=1234)
    binding = Mock()
    monkeypatch.setattr(shutdown, "saved_placement", Mock(return_value=(binding, path)))
    monkeypatch.setattr(shutdown.os, "pidfd_open", Mock(return_value=42), raising=False)
    monkeypatch.setattr(shutdown.os, "close", Mock())
    monkeypatch.setattr(shutdown, "process_identity", Mock(return_value="birth"))
    monkeypatch.setattr(shutdown.signal, "pidfd_send_signal", Mock(), raising=False)
    monkeypatch.setattr(shutdown.select, "select", Mock(return_value=([42], [], [])))
    cleanup = Mock()
    monkeypatch.setattr(shutdown, "cleanup_stopped_policy", cleanup)
    return vm, binding, path, cleanup


def test_stop_waits_before_cleanup(stopped):
    vm, binding, path, cleanup = stopped
    process = Mock()
    shutdown.stop_policy(vm, process, timeout=1)
    shutdown.signal.pidfd_send_signal.assert_called_once_with(42, shutdown.signal.SIGTERM)
    process.wait.assert_called_once_with(timeout=5)
    cleanup.assert_called_once_with(path, binding)


def test_recycled_pid_is_never_signalled(stopped):
    shutdown.process_identity.return_value = "somebody-else"
    shutdown.stop_policy(stopped[0], None, timeout=1)
    shutdown.signal.pidfd_send_signal.assert_not_called()
    stopped[3].assert_called_once()


def test_surviving_vm_keeps_placement(stopped):
    shutdown.select.select.return_value = ([], [], [])
    with pytest.raises(RuntimeError, match="did not stop"):
        shutdown.stop_policy(stopped[0], None, timeout=1)
    stopped[3].assert_not_called()


def test_incomplete_attachment_requires_owned_child(stopped):
    stopped[2].write_text("{}")
    with pytest.raises(RuntimeError, match="could not be verified"):
        shutdown.stop_policy(stopped[0], None, timeout=1)
    stopped[3].assert_not_called()
    child = Mock()
    shutdown.stop_policy(stopped[0], child, timeout=1)
    child.kill.assert_called_once()
    child.wait.assert_called_once_with(timeout=1)
    stopped[3].assert_called_once()


def test_failed_child_wait_retains_placement(stopped):
    child = Mock()
    child.wait.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        shutdown.stop_policy(stopped[0], child, timeout=1)
    stopped[3].assert_not_called()


@pytest.mark.parametrize("during_kill", [False, True])
def test_exit_racing_signal_still_waits_for_kernel_handle(stopped, during_kill):
    shutdown.signal.pidfd_send_signal.side_effect = (
        [None, ProcessLookupError()] if during_kill else ProcessLookupError()
    )
    shutdown.select.select.side_effect = (
        [([], [], []), ([42], [], [])] if during_kill else [([42], [], [])]
    )
    shutdown.stop_policy(stopped[0], None, timeout=1)
    stopped[3].assert_called_once()
    shutdown.os.close.assert_called_once_with(42)


def test_signal_exit_race_without_confirmed_death_keeps_placement(stopped):
    shutdown.signal.pidfd_send_signal.side_effect = ProcessLookupError()
    shutdown.select.select.return_value = ([], [], [])
    with pytest.raises(RuntimeError, match="did not stop"):
        shutdown.stop_policy(stopped[0], None, timeout=1)
    stopped[3].assert_not_called()


def test_different_recorded_pid_keeps_placement(stopped):
    stopped[0].pid = 5678
    with pytest.raises(RuntimeError, match="ownership changed"):
        shutdown.stop_policy(stopped[0], None, timeout=1)
    shutdown.os.pidfd_open.assert_not_called()
    stopped[3].assert_not_called()
