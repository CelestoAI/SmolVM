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

"""Tests for dormant host-to-guest browser proxy plumbing."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import MagicMock

from smolvm.browser import (
    _BrowserSandbox,
    _guest_browser_proxy_endpoint,
    _guest_browser_session_command,
)
from smolvm.types import (
    BrowserSessionConfig,
    CommandResult,
    NetworkConfig,
    VMConfig,
    VMInfo,
    VMState,
)


def _vm_info(
    tmp_path: Path,
    *,
    backend: str,
    qemu_network: Literal["slirp", "tap"] = "slirp",
    proxy_port: int | None,
) -> VMInfo:
    kernel = tmp_path / f"{backend}-kernel"
    rootfs = tmp_path / f"{backend}-rootfs"
    kernel.touch()
    rootfs.touch()
    return VMInfo(
        vm_id=f"browser-{backend}",
        status=VMState.CREATED,
        config=VMConfig(
            vm_id=f"browser-{backend}",
            kernel_path=kernel,
            rootfs_path=rootfs,
            backend=backend,
            qemu_network=qemu_network,
        ),
        network=NetworkConfig(
            guest_ip="172.16.0.9" if qemu_network == "tap" else "10.0.2.15",
            gateway_ip="172.16.0.1" if qemu_network == "tap" else "10.0.2.2",
            tap_device="tap9" if qemu_network == "tap" else "usernet",
            guest_mac="02:00:00:00:00:09",
            egress_proxy_host_port=proxy_port,
        ),
    )


def test_browser_session_command_preserves_legacy_shape_without_proxy() -> None:
    command = _guest_browser_session_command(
        "launch-browser",
        ["headless", "1280", "720", "9222", "/profile with space", "/downloads", "1"],
    )

    assert command == (
        "/usr/local/bin/smolvm-browser-session launch-browser headless 1280 720 9222 "
        "'/profile with space' /downloads 1"
    )


def test_browser_session_command_appends_one_quoted_proxy_argument() -> None:
    command = _guest_browser_session_command(
        "launch-browser",
        ["headless", "1280"],
        proxy_endpoint="http://10.0.2.100:3128",
    )

    assert command == (
        "/usr/local/bin/smolvm-browser-session launch-browser headless 1280 http://10.0.2.100:3128"
    )


def test_qemu_slirp_browser_uses_the_guestfwd_endpoint(tmp_path: Path) -> None:
    info = _vm_info(tmp_path, backend="qemu", proxy_port=43128)
    assert _guest_browser_proxy_endpoint(info) == "http://10.0.2.100:3128"


def test_tap_browser_uses_the_host_gateway_listener(tmp_path: Path) -> None:
    info = _vm_info(tmp_path, backend="qemu", qemu_network="tap", proxy_port=43128)
    assert _guest_browser_proxy_endpoint(info) == "http://172.16.0.1:43128"


def test_browser_has_no_proxy_argument_without_internal_wiring(tmp_path: Path) -> None:
    info = _vm_info(tmp_path, backend="qemu", proxy_port=None)
    assert _guest_browser_proxy_endpoint(info) is None


def test_browser_start_threads_the_internal_proxy_to_the_guest(tmp_path: Path) -> None:
    session = object.__new__(_BrowserSandbox)
    session._session_config = BrowserSessionConfig(session_id="browser-proxy")
    session._info = SimpleNamespace(session_id="browser-proxy")
    session._vm = MagicMock()
    session._vm.info = _vm_info(tmp_path, backend="qemu", proxy_port=43128)
    session._vm.run.return_value = CommandResult(exit_code=0, stdout="", stderr="")

    session._start_guest_browser()

    command = session._vm.run.call_args.args[0]
    assert command.startswith("/usr/local/bin/smolvm-browser-session start headless")
    assert command.endswith("http://10.0.2.100:3128")


def test_browser_relaunch_threads_the_same_internal_proxy(tmp_path: Path) -> None:
    session = object.__new__(_BrowserSandbox)
    session._session_config = BrowserSessionConfig(session_id="browser-proxy")
    session._info = SimpleNamespace(session_id="browser-proxy")
    session._vm = MagicMock()
    session._vm.info = _vm_info(tmp_path, backend="qemu", proxy_port=43128)
    session._vm.run.return_value = CommandResult(exit_code=0, stdout="", stderr="")

    session._launch_guest_browser()

    command = session._vm.run.call_args.args[0]
    assert command.startswith("/usr/local/bin/smolvm-browser-session launch-browser headless")
    assert command.endswith("http://10.0.2.100:3128")
