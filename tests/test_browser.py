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

"""Tests for browser session orchestration."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.browser import BrowserSession, _browser_vm_id, _build_browser_vm_config
from smolvm.exceptions import BrowserSessionNotFoundError
from smolvm.runtime.boot_profiles import KernelBootProfile
from smolvm.types import (
    BrowserSessionConfig,
    BrowserSessionState,
    CommandResult,
    PortForwardConfig,
    VMConfig,
    VMState,
)


@pytest.fixture
def sample_vm_config(tmp_path: Path) -> VMConfig:
    """Create a sample VMConfig for browser session tests."""
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()

    return VMConfig(
        vm_id="browser-abc123",
        kernel_path=kernel,
        rootfs_path=rootfs,
    )


def test_browser_vm_id_uses_stable_profile_id() -> None:
    """Persistent profiles should map to a stable VM identifier."""
    config = BrowserSessionConfig(profile_mode="persistent", profile_id="acct-1")

    vm_id = _browser_vm_id("browser-deadbeef", config)

    assert vm_id.startswith("browser-prof-acct-1-")


@patch("smolvm.utils.ensure_ssh_key")
@patch("smolvm.images.builder.ImageBuilder")
@patch("smolvm.browser._allocate_browser_host_port", side_effect=[39001])
def test_build_browser_vm_config_uses_persistent_disk_reuse(
    mock_allocate_host_port: MagicMock,
    mock_builder_cls: MagicMock,
    mock_ensure_ssh_key: MagicMock,
    tmp_path: Path,
) -> None:
    """Persistent profiles should retain their disk across recreated sessions."""
    kernel = tmp_path / "kernel"
    rootfs = tmp_path / "rootfs.ext4"
    private_key = tmp_path / "id_ed25519"
    public_key = tmp_path / "id_ed25519.pub"
    kernel.touch()
    rootfs.touch()
    private_key.touch()
    public_key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMock user@test\n")

    mock_ensure_ssh_key.return_value = (private_key, public_key)
    mock_builder = MagicMock()
    mock_builder.build_browser_rootfs.return_value = (kernel, rootfs)
    mock_builder.qemu_kernel_url_for_host.side_effect = AssertionError(
        "browser QEMU path should not use the desktop kernel helper"
    )
    mock_builder_cls.return_value = mock_builder

    browser_config = BrowserSessionConfig(
        session_id="browser-abc123",
        backend="qemu",
        profile_mode="persistent",
        profile_id="acct-1",
    )

    vm_config, ssh_key_path = _build_browser_vm_config(
        session_id="browser-abc123",
        browser_config=browser_config,
    )

    assert vm_config.retain_disk_on_delete is True
    assert vm_config.backend == "qemu"
    assert vm_config.vm_id.startswith("browser-prof-acct-1-")
    assert ssh_key_path == str(private_key)
    assert len(vm_config.port_forwards) == 1
    assert vm_config.port_forwards[0].host_port == 39001
    assert vm_config.port_forwards[0].guest_port == 9222
    mock_builder.build_browser_rootfs.assert_called_once()
    assert (
        mock_builder.build_browser_rootfs.call_args.kwargs["kernel_profile"]
        == KernelBootProfile.MICROVM_DIRECT
    )
    assert "kernel_url" not in mock_builder.build_browser_rootfs.call_args.kwargs
    mock_allocate_host_port.assert_called_once()


@patch("smolvm.utils.ensure_ssh_key")
@patch("smolvm.images.builder.ImageBuilder")
@patch("smolvm.browser._allocate_browser_host_port", side_effect=[39011, 39012])
def test_build_browser_vm_config_allocates_qemu_live_port_forwards(
    mock_allocate_host_port: MagicMock,
    mock_builder_cls: MagicMock,
    mock_ensure_ssh_key: MagicMock,
    tmp_path: Path,
) -> None:
    """Live QEMU sessions should preallocate host forwards for CDP and noVNC."""
    kernel = tmp_path / "kernel"
    rootfs = tmp_path / "rootfs.ext4"
    private_key = tmp_path / "id_ed25519"
    public_key = tmp_path / "id_ed25519.pub"
    kernel.touch()
    rootfs.touch()
    private_key.touch()
    public_key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMock user@test\n")

    mock_ensure_ssh_key.return_value = (private_key, public_key)
    mock_builder = MagicMock()
    mock_builder.build_browser_rootfs.return_value = (kernel, rootfs)
    mock_builder_cls.return_value = mock_builder

    browser_config = BrowserSessionConfig(
        session_id="browser-live123",
        backend="qemu",
        mode="live",
    )

    vm_config, _ = _build_browser_vm_config(
        session_id="browser-live123",
        browser_config=browser_config,
    )

    assert [(forward.host_port, forward.guest_port) for forward in vm_config.port_forwards] == [
        (39011, 9222),
        (39012, 6080),
    ]
    assert mock_allocate_host_port.call_count == 2


@patch("smolvm.utils.ensure_ssh_key")
@patch("smolvm.images.builder.ImageBuilder")
def test_build_browser_vm_config_passes_pubkey_to_vmconfig(
    mock_builder_cls: MagicMock,
    mock_ensure_ssh_key: MagicMock,
    tmp_path: Path,
) -> None:
    """Browser VMConfig must carry the user's pubkey so /init injects it at boot.

    Browser images no longer bake authorized_keys at build time
    (see src/smolvm/images/builder.py build_browser_rootfs); the key is
    delivered via the kernel cmdline, which only fires when ssh_public_key
    is set on VMConfig.
    """
    kernel = tmp_path / "kernel"
    rootfs = tmp_path / "rootfs.ext4"
    private_key = tmp_path / "id_ed25519"
    public_key = tmp_path / "id_ed25519.pub"
    kernel.touch()
    rootfs.touch()
    private_key.touch()
    pubkey_value = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKey user@test"
    public_key.write_text(f"{pubkey_value}\n")

    mock_ensure_ssh_key.return_value = (private_key, public_key)
    mock_builder = MagicMock()
    mock_builder.build_browser_rootfs.return_value = (kernel, rootfs)
    mock_builder_cls.return_value = mock_builder

    vm_config, _ = _build_browser_vm_config(
        session_id="browser-key-test",
        browser_config=BrowserSessionConfig(session_id="browser-key-test"),
    )

    assert vm_config.ssh_public_key == pubkey_value


@patch("smolvm.browser.SmolVM")
@patch("smolvm.browser._build_browser_vm_config")
def test_browser_session_start_persists_ready_state(
    mock_build_browser_vm_config: MagicMock,
    mock_vm_cls: MagicMock,
    sample_vm_config: VMConfig,
    tmp_path: Path,
) -> None:
    """Starting a browser session should expose CDP/live URLs and persist READY state."""
    mock_build_browser_vm_config.return_value = (sample_vm_config, str(tmp_path / "id_ed25519"))

    vm = MagicMock()
    vm.vm_id = "browser-abc123"
    vm.status = VMState.CREATED
    vm.expose_local.side_effect = [39222, 36080]

    def _run_side_effect(command: str, timeout: int = 30, shell: str = "login") -> CommandResult:
        if command.startswith("/usr/local/bin/smolvm-browser-wait-port 9222 1.0"):
            return CommandResult(exit_code=1, stdout="", stderr="")
        if command.startswith("/usr/local/bin/smolvm-browser-session start"):
            return CommandResult(exit_code=0, stdout="", stderr="")
        if command.startswith("/usr/local/bin/smolvm-browser-wait-port 9222"):
            return CommandResult(exit_code=0, stdout="", stderr="")
        if command.startswith("/usr/local/bin/smolvm-browser-wait-port 6080"):
            return CommandResult(exit_code=0, stdout="", stderr="")
        return CommandResult(exit_code=0, stdout="", stderr="")

    vm.run.side_effect = _run_side_effect
    mock_vm_cls.return_value = vm

    session = BrowserSession(
        BrowserSessionConfig(
            session_id="browser-abc123",
            mode="live",
            record_video=True,
        ),
        data_dir=tmp_path,
    )

    session.start()

    assert session.status == BrowserSessionState.READY
    assert session.cdp_url == "http://127.0.0.1:39222"
    assert session.live_url == "http://127.0.0.1:36080/vnc.html?autoconnect=1&resize=scale"
    persisted = session.refresh().info
    assert persisted.status == BrowserSessionState.READY
    assert persisted.debug_port == 39222
    session.close()


@patch("smolvm.browser.SmolVM")
@patch("smolvm.browser._build_browser_vm_config")
def test_browser_session_start_uses_expose_local_for_qemu_cdp(
    mock_build_browser_vm_config: MagicMock,
    mock_vm_cls: MagicMock,
    sample_vm_config: VMConfig,
    tmp_path: Path,
) -> None:
    """QEMU browser sessions should expose CDP through the localhost helper path."""
    qemu_vm_config = sample_vm_config.model_copy(
        update={
            "backend": "qemu",
            "port_forwards": [PortForwardConfig(host_port=39001, guest_port=9222)],
        }
    )
    mock_build_browser_vm_config.return_value = (qemu_vm_config, str(tmp_path / "id_ed25519"))

    vm = MagicMock()
    vm.vm_id = "browser-abc123"
    vm.status = VMState.CREATED
    vm.expose_local.return_value = 39222

    def _run_side_effect(command: str, timeout: int = 30, shell: str = "login") -> CommandResult:
        del timeout, shell
        if command.startswith("/usr/local/bin/smolvm-browser-wait-port 9222 1.0"):
            return CommandResult(exit_code=1, stdout="", stderr="")
        if command.startswith("/usr/local/bin/smolvm-browser-session start"):
            return CommandResult(exit_code=0, stdout="", stderr="")
        if command.startswith("/usr/local/bin/smolvm-browser-wait-port 9222"):
            return CommandResult(exit_code=0, stdout="", stderr="")
        return CommandResult(exit_code=0, stdout="", stderr="")

    vm.run.side_effect = _run_side_effect
    mock_vm_cls.return_value = vm

    session = BrowserSession(
        BrowserSessionConfig(session_id="browser-abc123", backend="qemu"),
        data_dir=tmp_path,
    )

    session.start()

    assert session.cdp_url == "http://127.0.0.1:39222"
    vm.expose_local.assert_called_once_with(guest_port=9222)
    session.close()


@patch("smolvm.browser.SmolVM")
@patch("smolvm.browser._build_browser_vm_config")
@patch.object(BrowserSession, "collect_artifacts", return_value=Path("/tmp/guest-artifacts.tar.gz"))
def test_browser_session_stop_deletes_state_record(
    _mock_collect_artifacts: MagicMock,
    mock_build_browser_vm_config: MagicMock,
    mock_vm_cls: MagicMock,
    sample_vm_config: VMConfig,
    tmp_path: Path,
) -> None:
    """Stopping a browser session should delete its persisted session record."""
    mock_build_browser_vm_config.return_value = (sample_vm_config, str(tmp_path / "id_ed25519"))

    vm = MagicMock()
    vm.vm_id = "browser-abc123"
    vm.status = VMState.RUNNING
    vm.run.return_value = CommandResult(exit_code=0, stdout="", stderr="")
    mock_vm_cls.return_value = vm

    session = BrowserSession(
        BrowserSessionConfig(session_id="browser-abc123"),
        data_dir=tmp_path,
    )
    session.stop()

    with pytest.raises(BrowserSessionNotFoundError):
        session.refresh()
