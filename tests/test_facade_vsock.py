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

"""Facade-level tests for the vsock control-channel seam.

These build a SmolVM via ``__new__`` (bypassing the SDK/create path) and drive
``_wait_for_ssh`` directly, so they validate channel resolution + dispatch
without booting a VM.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from smolvm.comm.vsock_channel import VsockChannel
from smolvm.exceptions import OperationTimeoutError
from smolvm.facade import SmolVM
from smolvm.types import GuestOS, VMConfig, VMInfo, VMState, VsockConfig


def _vsock_vm(tmp_path: Path, *, comm_channel: str | None, request: str | None) -> SmolVM:
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()
    config = VMConfig(
        vm_id="vm1",
        kernel_path=kernel,
        rootfs_path=rootfs,
        backend="qemu",
        guest_os=GuestOS.ALPINE,
        boot_args="console=ttyS0 reboot=k panic=1 init=/init",
        comm_channel=comm_channel,
        vsock=VsockConfig(guest_cid=5),
    )
    info = VMInfo(vm_id="vm1", status=VMState.RUNNING, config=config)

    vm = SmolVM.__new__(SmolVM)
    vm._comm_channel_request = request
    vm._vm_id = "vm1"
    vm._ssh = None
    vm._ssh_ready = False
    vm._info = info
    vm._sdk = MagicMock()
    vm._sdk.get.return_value = info
    return vm


def test_wait_for_ssh_uses_vsock_when_agent_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("smolvm.comm.select.host_supports_vsock", lambda: True)
    monkeypatch.setattr(VsockChannel, "wait_ready", lambda self, timeout=60.0, interval=0.1: None)

    vm = _vsock_vm(tmp_path, comm_channel="vsock", request=None)
    vm._wait_for_ssh(timeout=5)

    assert vm._ssh_ready is True
    assert isinstance(vm._ssh, VsockChannel)
    assert vm._ssh.guest_cid == 5


def test_explicit_vsock_does_not_fall_back_to_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("smolvm.comm.select.host_supports_vsock", lambda: True)

    def _fail(self, timeout=60.0, interval=0.1):
        raise OperationTimeoutError("vsock", timeout)

    monkeypatch.setattr(VsockChannel, "wait_ready", _fail)

    # request="vsock" → explicit, allow_fallback=False
    vm = _vsock_vm(tmp_path, comm_channel="vsock", request="vsock")
    with pytest.raises(OperationTimeoutError):
        vm._wait_for_ssh(timeout=1)
    assert vm._ssh_ready is False


def test_auto_vsock_falls_back_to_ssh_within_probe_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent-less image (vsock never answers) must fall back to SSH and only
    spend up to _VSOCK_AUTO_PROBE_TIMEOUT probing, not the full call timeout."""
    import smolvm.facade as facade

    monkeypatch.setattr("smolvm.comm.select.host_supports_vsock", lambda: True)

    seen = {}

    def _fail(self, timeout=60.0, interval=0.1):
        seen["probe_timeout"] = timeout
        raise OperationTimeoutError("vsock", timeout)

    monkeypatch.setattr(VsockChannel, "wait_ready", _fail)

    ssh_called = {}

    def _ssh_ok(self, timeout):
        ssh_called["timeout"] = timeout
        self._ssh_ready = True

    monkeypatch.setattr(SmolVM, "_wait_for_ssh_over_network", _ssh_ok)

    # auto channel (comm_channel=None, request=None) → vsock with fallback
    vm = _vsock_vm(tmp_path, comm_channel=None, request=None)
    vm._wait_for_ssh(timeout=30)

    # vsock probe was capped at the (short) auto budget, not the 30s call timeout
    assert seen["probe_timeout"] == facade._VSOCK_AUTO_PROBE_TIMEOUT
    assert facade._VSOCK_AUTO_PROBE_TIMEOUT <= 3.0  # guardrail stays short
    # and we then fell back to SSH
    assert ssh_called  # SSH path was taken
    assert vm._ssh_ready is True


def test_resolve_channel_reads_config_and_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("smolvm.comm.select.host_supports_vsock", lambda: True)
    vm = _vsock_vm(tmp_path, comm_channel=None, request=None)
    # auto on linux+qemu → vsock with fallback allowed
    res = vm._resolve_channel()
    assert res.kind == "vsock"
    assert res.allow_fallback is True
