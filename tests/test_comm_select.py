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

"""Tests for comm-channel resolution."""

import pytest

from smolvm.comm.select import resolve_comm_channel
from smolvm.exceptions import SmolVMError
from smolvm.runtime.backends import BACKEND_FIRECRACKER, BACKEND_QEMU
from smolvm.types import GuestOS


def _resolve(**overrides):
    base = {
        "requested": None,
        "config_channel": None,
        "backend": BACKEND_QEMU,
        "guest_os": GuestOS.ALPINE,
        "host_vsock_supported": True,
    }
    base.update(overrides)
    return resolve_comm_channel(**base)


class TestAuto:
    def test_auto_picks_vsock_with_fallback_on_linux_qemu(self) -> None:
        res = _resolve()
        assert res.kind == "vsock"
        assert res.allow_fallback is True

    def test_auto_falls_back_to_ssh_without_host_vsock(self) -> None:
        res = _resolve(host_vsock_supported=False)
        assert res.kind == "ssh"
        assert res.allow_fallback is False

    def test_auto_uses_ssh_on_non_qemu_backend(self) -> None:
        res = _resolve(backend=BACKEND_FIRECRACKER)
        assert res.kind == "ssh"

    def test_auto_uses_ssh_for_windows(self) -> None:
        res = _resolve(guest_os=GuestOS.WINDOWS, backend=BACKEND_QEMU)
        assert res.kind == "ssh"


class TestExplicit:
    def test_explicit_ssh(self) -> None:
        res = _resolve(requested="ssh")
        assert res.kind == "ssh"
        assert res.allow_fallback is False

    def test_explicit_vsock_no_fallback(self) -> None:
        res = _resolve(requested="vsock")
        assert res.kind == "vsock"
        assert res.allow_fallback is False

    def test_explicit_vsock_on_macos_raises(self) -> None:
        with pytest.raises(SmolVMError, match="vhost_vsock"):
            _resolve(requested="vsock", host_vsock_supported=False)

    def test_explicit_vsock_on_non_qemu_raises(self) -> None:
        with pytest.raises(SmolVMError, match="QEMU"):
            _resolve(requested="vsock", backend=BACKEND_FIRECRACKER)

    def test_explicit_vsock_on_windows_raises(self) -> None:
        with pytest.raises(SmolVMError, match="Windows"):
            _resolve(requested="vsock", guest_os=GuestOS.WINDOWS)

    def test_request_overrides_config(self) -> None:
        # Explicit ssh beats a vsock preference stored on the config.
        res = _resolve(requested="ssh", config_channel="vsock")
        assert res.kind == "ssh"

    def test_config_channel_used_when_no_request(self) -> None:
        res = _resolve(requested=None, config_channel="ssh")
        assert res.kind == "ssh"
