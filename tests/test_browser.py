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

from smolvm import SmolVM
from smolvm.browser import (
    _browser_vm_id,
    _BrowserSandbox,
    _build_browser_vm_config,
    _DesktopSandbox,
)
from smolvm.exceptions import BrowserSessionNotFoundError
from smolvm.runtime.boot_profiles import KernelBootProfile
from smolvm.types import (
    BrowserSessionConfig,
    BrowserSessionState,
    CommandResult,
    PortForwardConfig,
    VMConfig,
    VMState,
    WorkspaceMount,
)


class _CdpResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self) -> "_CdpResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


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


@patch("smolvm.browser._BrowserSandbox")
def test_smolvm_browser_factory_starts_headless_sandbox(mock_sandbox_cls: MagicMock) -> None:
    """SmolVM.browser(headless=True) should start a CDP-only browser sandbox."""
    sandbox = MagicMock()
    mock_sandbox_cls.return_value = sandbox

    result = SmolVM.browser(
        headless=True,
        viewport={"width": 1024, "height": 768},
        boot_timeout=12.5,
    )

    assert result is sandbox
    config = mock_sandbox_cls.call_args.args[0]
    assert config.mode == "headless"
    assert config.viewport_width == 1024
    assert config.viewport_height == 768
    session_kwargs = mock_sandbox_cls.call_args.kwargs
    assert session_kwargs == {
        "data_dir": None,
        "socket_dir": None,
        "ssh_key_path": None,
    }
    sandbox.start.assert_called_once_with(boot_timeout=12.5, on_progress=None)


@patch("smolvm.browser._BrowserSandbox")
def test_smolvm_browser_factory_starts_visible_sandbox(mock_sandbox_cls: MagicMock) -> None:
    """SmolVM.browser(headless=False) should start a visible browser sandbox."""
    sandbox = MagicMock()
    mock_sandbox_cls.return_value = sandbox

    result = SmolVM.browser(headless=False, record_video=True)

    assert result is sandbox
    config = mock_sandbox_cls.call_args.args[0]
    assert config.mode == "live"
    assert config.record_video is True
    sandbox.start.assert_called_once_with(boot_timeout=90.0, on_progress=None)


@patch("smolvm.browser._DesktopSandbox")
def test_smolvm_desktop_factory_starts_visible_sandbox(mock_sandbox_cls: MagicMock) -> None:
    """SmolVM.desktop() should start a visible desktop sandbox."""
    sandbox = MagicMock()
    mock_sandbox_cls.return_value = sandbox

    result = SmolVM.desktop(viewport_width=1440, viewport_height=900)

    assert result is sandbox
    config = mock_sandbox_cls.call_args.args[0]
    assert config.mode == "desktop"
    assert config.viewport_width == 1440
    assert config.viewport_height == 900
    sandbox.start.assert_called_once_with(boot_timeout=90.0, on_progress=None)


@patch("smolvm.browser._BrowserSandbox")
def test_smolvm_browser_factory_stops_sandbox_on_start_failure(
    mock_sandbox_cls: MagicMock,
) -> None:
    """SmolVM.browser() should not leave a created sandbox around after start fails."""
    sandbox = MagicMock()
    sandbox.start.side_effect = RuntimeError("boom")
    mock_sandbox_cls.return_value = sandbox

    with pytest.raises(RuntimeError, match="boom"):
        SmolVM.browser()

    sandbox.stop.assert_called_once_with()


@patch("smolvm.facade.logger")
@patch("smolvm.browser._BrowserSandbox")
def test_smolvm_browser_factory_logs_cleanup_failure_without_replacing_start_error(
    mock_sandbox_cls: MagicMock,
    mock_logger: MagicMock,
) -> None:
    """Cleanup errors should be logged without hiding the original start failure."""
    sandbox = MagicMock()
    sandbox.start.side_effect = RuntimeError("start failed")
    sandbox.stop.side_effect = RuntimeError("stop failed")
    mock_sandbox_cls.return_value = sandbox

    with pytest.raises(RuntimeError, match="start failed"):
        SmolVM.browser()

    sandbox.stop.assert_called_once_with()
    mock_logger.exception.assert_called_once_with(
        "Failed to clean up display sandbox after startup failed."
    )


@patch("smolvm.browser._BrowserSandbox")
def test_smolvm_browser_factory_rejects_invalid_resource_limits(
    mock_sandbox_cls: MagicMock,
) -> None:
    """Invalid factory limits should fail before constructing the sandbox."""
    with pytest.raises(ValueError, match="memory_mb"):
        SmolVM.browser(memory_mb=0)
    with pytest.raises(ValueError, match="disk_size_mb"):
        SmolVM.browser(disk_size_mb=0)
    with pytest.raises(ValueError, match="timeout_minutes"):
        SmolVM.browser(timeout_minutes=0)
    with pytest.raises(ValueError, match="boot_timeout"):
        SmolVM.browser(boot_timeout=0)

    mock_sandbox_cls.assert_not_called()


@patch("smolvm.browser._DesktopSandbox")
def test_smolvm_desktop_factory_rejects_invalid_viewport(
    mock_sandbox_cls: MagicMock,
) -> None:
    """Viewport values should be validated before constructing the sandbox."""
    with pytest.raises(ValueError, match="viewport.width"):
        SmolVM.desktop(viewport={"width": 0, "height": 900})
    with pytest.raises(ValueError, match="viewport_height"):
        SmolVM.desktop(viewport_height=-1)

    mock_sandbox_cls.assert_not_called()


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
    mock_builder.qemu_kernel_url_for_host.return_value = "https://example.invalid/kernel.image"
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
    assert vm_config.comm_channel is None
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
    assert (
        mock_builder.build_browser_rootfs.call_args.kwargs["kernel_url"]
        == "https://example.invalid/kernel.image"
    )
    mock_builder.qemu_kernel_url_for_host.assert_called_once_with()
    mock_allocate_host_port.assert_called_once()


@patch("smolvm.utils.ensure_ssh_key")
@patch("smolvm.images.builder.ImageBuilder")
def test_build_browser_vm_config_passes_workspace_mounts_and_selects_qemu(
    mock_builder_cls: MagicMock,
    mock_ensure_ssh_key: MagicMock,
    tmp_path: Path,
) -> None:
    """Browser sessions with host mounts should run on QEMU and pass mounts through."""
    kernel = tmp_path / "kernel"
    rootfs = tmp_path / "rootfs.ext4"
    private_key = tmp_path / "id_ed25519"
    public_key = tmp_path / "id_ed25519.pub"
    mounted = tmp_path / "demo"
    kernel.touch()
    rootfs.touch()
    private_key.touch()
    mounted.mkdir()
    public_key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMock user@test\n")

    mock_ensure_ssh_key.return_value = (private_key, public_key)
    mock_builder = MagicMock()
    mock_builder.build_browser_rootfs.return_value = (kernel, rootfs)
    mock_builder.qemu_kernel_url_for_host.return_value = "https://example.invalid/kernel.image"
    mock_builder_cls.return_value = mock_builder

    mount = WorkspaceMount(
        host_path=mounted,
        guest_path="/workspace/legacy_report_fetcher",
        writable=True,
    )
    browser_config = BrowserSessionConfig(
        session_id="browser-mounted",
        backend="auto",
        workspace_mounts=[mount],
    )

    vm_config, _ = _build_browser_vm_config(
        session_id="browser-mounted",
        browser_config=browser_config,
    )

    assert vm_config.backend == "qemu"
    assert vm_config.workspace_mounts == [mount]
    assert (
        mock_builder.build_browser_rootfs.call_args.kwargs["kernel_url"]
        == "https://example.invalid/kernel.image"
    )


@patch("smolvm.utils.ensure_ssh_key")
@patch("smolvm.images.builder.ImageBuilder")
@patch("smolvm.browser._allocate_browser_host_port", side_effect=[39011, 39012, 39013])
def test_build_browser_vm_config_allocates_qemu_live_port_forwards(
    mock_allocate_host_port: MagicMock,
    mock_builder_cls: MagicMock,
    mock_ensure_ssh_key: MagicMock,
    tmp_path: Path,
) -> None:
    """Live QEMU sessions should preallocate host forwards for CDP, noVNC, and VNC."""
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
    mock_builder.qemu_kernel_url_for_host.return_value = "https://example.invalid/kernel.image"
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
        (39013, 5900),
    ]
    assert mock_allocate_host_port.call_count == 3


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
    assert mock_builder.build_browser_rootfs.call_args.args[0] == pubkey_value


@patch("smolvm.utils.ensure_ssh_key")
@patch("smolvm.images.builder.ImageBuilder")
def test_build_browser_vm_config_uses_custom_key_public_half(
    mock_builder_cls: MagicMock,
    mock_ensure_ssh_key: MagicMock,
    tmp_path: Path,
) -> None:
    """Custom SSH fallback keys should provision their matching public key."""
    kernel = tmp_path / "kernel"
    rootfs = tmp_path / "rootfs.ext4"
    default_private_key = tmp_path / "default_id_ed25519"
    default_public_key = tmp_path / "default_id_ed25519.pub"
    custom_private_key = tmp_path / "custom_id_ed25519"
    custom_public_key = tmp_path / "custom_id_ed25519.pub"
    kernel.touch()
    rootfs.touch()
    default_private_key.touch()
    default_public_key.write_text("ssh-ed25519 AAAADefault user@test\n")
    custom_private_key.touch()
    custom_pubkey_value = "ssh-ed25519 AAAACustom user@test"
    custom_public_key.write_text(f"{custom_pubkey_value}\n")

    mock_ensure_ssh_key.return_value = (default_private_key, default_public_key)
    mock_builder = MagicMock()
    mock_builder.build_browser_rootfs.return_value = (kernel, rootfs)
    mock_builder_cls.return_value = mock_builder

    vm_config, ssh_key_path = _build_browser_vm_config(
        session_id="browser-custom-key",
        browser_config=BrowserSessionConfig(session_id="browser-custom-key"),
        ssh_key_path=str(custom_private_key),
    )

    assert ssh_key_path == str(custom_private_key)
    assert vm_config.ssh_public_key == custom_pubkey_value
    assert mock_builder.build_browser_rootfs.call_args.args[0] == custom_pubkey_value


@patch("smolvm.browser.SmolVM")
@patch("smolvm.browser._build_browser_vm_config")
@patch("smolvm.browser._LOCAL_HTTP_OPENER.open", return_value=_CdpResponse())
def test_browser_session_start_persists_ready_state(
    mock_open: MagicMock,
    mock_build_browser_vm_config: MagicMock,
    mock_vm_cls: MagicMock,
    sample_vm_config: VMConfig,
    tmp_path: Path,
) -> None:
    """Starting a browser sandbox should expose CDP/viewer/display URLs."""
    mock_build_browser_vm_config.return_value = (sample_vm_config, str(tmp_path / "id_ed25519"))

    vm = MagicMock()
    vm.vm_id = "browser-abc123"
    vm.status = VMState.CREATED
    vm.expose_local.side_effect = [39222, 36080, 35900]
    vm.wait_for_guest_tcp_ports.side_effect = [False, True, True, True, True, True]

    def _run_side_effect(command: str, timeout: int = 30, shell: str = "login") -> CommandResult:
        if command.startswith("/usr/local/bin/smolvm-browser-session start"):
            return CommandResult(exit_code=0, stdout="", stderr="")
        return CommandResult(exit_code=0, stdout="", stderr="")

    vm.run.side_effect = _run_side_effect
    mock_vm_cls.return_value = vm

    session = _BrowserSandbox(
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
    assert session.browser_cdp_url == "http://127.0.0.1:39222"
    mock_open.assert_called_once()
    assert session.viewer_url == "http://127.0.0.1:36080/vnc.html?autoconnect=1&resize=scale"
    assert session.display_url == "vnc://127.0.0.1:35900"
    assert session.vm is vm
    persisted = session.refresh().info
    assert persisted.status == BrowserSessionState.READY
    assert persisted.debug_port == 39222
    assert persisted.vnc_port == 35900
    assert persisted.vnc_url == "vnc://127.0.0.1:35900"
    assert [call.kwargs for call in vm.expose_local.call_args_list] == [
        {"guest_port": 9222, "guest_loopback": False},
        {"guest_port": 6080, "guest_loopback": False},
        {"guest_port": 5900, "guest_loopback": True},
    ]
    session.close()


@patch("smolvm.browser.SmolVM")
@patch("smolvm.browser._build_browser_vm_config")
@patch("smolvm.browser._BrowserSandbox._probe_local_port", return_value=True)
@patch("smolvm.browser.time.sleep")
@patch("smolvm.browser._LOCAL_HTTP_OPENER.open")
def test_browser_sandbox_start_uses_configured_qemu_cdp_forward(
    mock_open: MagicMock,
    _mock_sleep: MagicMock,
    mock_probe_local_port: MagicMock,
    mock_build_browser_vm_config: MagicMock,
    mock_vm_cls: MagicMock,
    sample_vm_config: VMConfig,
    tmp_path: Path,
) -> None:
    """QEMU browser sandboxes should reuse their preconfigured CDP host forward."""
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
    vm.info.config = qemu_vm_config
    vm.expose_local.return_value = 39222
    vm.wait_for_guest_tcp_ports.side_effect = [False, True]

    def _run_side_effect(command: str, timeout: int = 30, shell: str = "login") -> CommandResult:
        del timeout, shell
        if command.startswith("/usr/local/bin/smolvm-browser-session start"):
            return CommandResult(exit_code=0, stdout="", stderr="")
        return CommandResult(exit_code=0, stdout="", stderr="")

    vm.run.side_effect = _run_side_effect
    mock_vm_cls.return_value = vm
    mock_open.side_effect = [
        ConnectionResetError("reset"),
        _CdpResponse(status=503),
        _CdpResponse(),
    ]

    session = _BrowserSandbox(
        BrowserSessionConfig(session_id="browser-abc123", backend="qemu"),
        data_dir=tmp_path,
    )

    session.start()

    assert session.cdp_url == "http://127.0.0.1:39001"
    assert mock_open.call_count == 3
    mock_probe_local_port.assert_called_once_with(39001)
    vm.expose_local.assert_not_called()
    session.close()


def test_browser_wait_for_guest_port_uses_facade_control_wait() -> None:
    """Browser orchestration should use the SmolVM facade for protocol port waits."""
    vm = MagicMock()
    vm.wait_for_guest_tcp_ports.return_value = True
    session = object.__new__(_BrowserSandbox)
    session._vm = vm

    assert session._wait_for_guest_port(9222, timeout=2.5) is True

    vm.wait_for_guest_tcp_ports.assert_called_once_with(
        [9222],
        timeout=2.5,
        host="127.0.0.1",
    )
    vm.run.assert_not_called()


def test_browser_wait_for_guest_port_falls_back_when_control_wait_unsupported() -> None:
    """Legacy channels should still use the browser image's wait-port script."""
    vm = MagicMock()
    vm.wait_for_guest_tcp_ports.return_value = None
    vm.run.return_value = CommandResult(exit_code=0, stdout="", stderr="")
    session = object.__new__(_BrowserSandbox)
    session._vm = vm

    assert session._wait_for_guest_port(9222, timeout=2.5) is True

    vm.run.assert_called_once_with(
        "/usr/local/bin/smolvm-browser-wait-port 9222 2.5",
        timeout=7,
    )


def test_browser_wait_for_guest_port_does_not_fallback_after_control_timeout() -> None:
    """A real protocol timeout should not spend the deadline again via shell fallback."""
    vm = MagicMock()
    vm.wait_for_guest_tcp_ports.return_value = False
    session = object.__new__(_BrowserSandbox)
    session._vm = vm

    assert session._wait_for_guest_port(9222, timeout=2.5) is False

    vm.run.assert_not_called()


@patch("smolvm.browser.SmolVM")
@patch("smolvm.browser._build_browser_vm_config")
def test_desktop_sandbox_start_exposes_viewer_and_display_only(
    mock_build_browser_vm_config: MagicMock,
    mock_vm_cls: MagicMock,
    sample_vm_config: VMConfig,
    tmp_path: Path,
) -> None:
    """Desktop sandboxes should expose display URLs without a CDP endpoint."""
    mock_build_browser_vm_config.return_value = (sample_vm_config, str(tmp_path / "id_ed25519"))

    vm = MagicMock()
    vm.vm_id = "desktop-abc123"
    vm.status = VMState.CREATED
    vm.expose_local.side_effect = [36080, 35900]
    vm.wait_for_guest_tcp_ports.side_effect = [False, True, True]

    def _run_side_effect(command: str, timeout: int = 30, shell: str = "login") -> CommandResult:
        del timeout, shell
        if command.startswith("/usr/local/bin/smolvm-browser-wait-port 9222"):
            raise AssertionError("desktop mode should not wait for CDP")
        if command.startswith("/usr/local/bin/smolvm-browser-session start"):
            assert " desktop " in command
            return CommandResult(exit_code=0, stdout="", stderr="")
        if command.startswith("/usr/local/bin/smolvm-browser-wait-port 6080"):
            return CommandResult(exit_code=0, stdout="", stderr="")
        if command.startswith("/usr/local/bin/smolvm-browser-wait-port 5900"):
            return CommandResult(exit_code=0, stdout="", stderr="")
        return CommandResult(exit_code=0, stdout="", stderr="")

    vm.run.side_effect = _run_side_effect
    mock_vm_cls.return_value = vm

    session = _DesktopSandbox(
        BrowserSessionConfig(session_id="desktop-abc123", mode="desktop"),
        data_dir=tmp_path,
    )

    session.start()

    assert session.cdp_url is None
    assert session.viewer_url == "http://127.0.0.1:36080/vnc.html?autoconnect=1&resize=scale"
    assert session.display_url == "vnc://127.0.0.1:35900"
    assert [call.kwargs for call in vm.expose_local.call_args_list] == [
        {"guest_port": 6080, "guest_loopback": False},
        {"guest_port": 5900, "guest_loopback": True},
    ]
    session.close()


@patch("smolvm.browser.SmolVM")
@patch("smolvm.browser._build_browser_vm_config")
@patch.object(
    _BrowserSandbox,
    "collect_artifacts",
)
def test_browser_sandbox_stop_deletes_state_record(
    _mock_collect_artifacts: MagicMock,
    mock_build_browser_vm_config: MagicMock,
    mock_vm_cls: MagicMock,
    sample_vm_config: VMConfig,
    tmp_path: Path,
) -> None:
    """Stopping a browser sandbox should delete its persisted state record."""
    _mock_collect_artifacts.return_value = tmp_path / "guest-artifacts.tar.gz"
    mock_build_browser_vm_config.return_value = (sample_vm_config, str(tmp_path / "id_ed25519"))

    vm = MagicMock()
    vm.vm_id = "browser-abc123"
    vm.status = VMState.RUNNING
    vm.run.return_value = CommandResult(exit_code=0, stdout="", stderr="")
    mock_vm_cls.return_value = vm

    session = _BrowserSandbox(
        BrowserSessionConfig(session_id="browser-abc123"),
        data_dir=tmp_path,
    )
    session.stop()

    with pytest.raises(BrowserSessionNotFoundError):
        session.refresh()
