# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from smolvm.exceptions import SmolVMError
from smolvm.facade import SmolVM, _build_auto_config
from smolvm.types import (
    DesktopEndpoint,
    GuestOS,
    MacOSMachineConfig,
    VMConfig,
    VMInfo,
    VMState,
    WorkspaceMount,
)


def _info(tmp_path: Path, *, status: VMState = VMState.RUNNING) -> VMInfo:
    bundle = tmp_path / "storage" / "mac-test"
    bundle.mkdir(parents=True)
    (bundle / ".smolvm-vnc-password").write_text("secret\n")
    config = VMConfig(
        vm_id="mac-test",
        guest_os=GuestOS.MACOS,
        backend="vz",
        boot_mode="platform",
        macos_machine=MacOSMachineConfig(
            base_image="macos-latest",
            manifest_path=tmp_path / "manifest.json",
            bundle_path=bundle,
            guest_version="26.0",
        ),
    )
    return VMInfo(
        vm_id="mac-test",
        status=status,
        config=config,
        display=DesktopEndpoint(port=5901) if status is VMState.RUNNING else None,
    )


def _facade(info: VMInfo) -> SmolVM:
    vm = SmolVM.__new__(SmolVM)
    vm._vm_id = info.vm_id
    vm._info = info
    vm._sdk = MagicMock()
    vm._sdk.get.return_value = info
    return vm


def test_auto_config_builds_macos_platform_vm(tmp_path: Path) -> None:
    machine = MacOSMachineConfig(
        base_image="macos-latest",
        manifest_path=tmp_path / "manifest.json",
        bundle_path=tmp_path / "data" / "macos-vms" / "mac-test",
        guest_version="26.0",
    )
    manager = MagicMock()
    manager.get.return_value = SimpleNamespace(cpu_count=4, memory_mib=8192)
    manager.machine_config.return_value = machine

    with (
        patch("smolvm.facade.ensure_backend_available"),
        patch("smolvm.macos.images.MacOSImageManager", return_value=manager),
    ):
        config, key_path = _build_auto_config(
            vm_name="mac-test",
            os="macos",
            data_dir=tmp_path / "data",
        )

    assert config.guest_os is GuestOS.MACOS
    assert config.backend == "vz"
    assert config.boot_mode == "platform"
    assert config.macos_machine == machine
    assert key_path is None


def test_auto_config_rejects_macos_memory_that_bundle_cannot_apply(tmp_path: Path) -> None:
    manager = MagicMock()
    manager.get.return_value = SimpleNamespace(cpu_count=4, memory_mib=8192)
    with (
        patch("smolvm.facade.ensure_backend_available"),
        patch("smolvm.macos.images.MacOSImageManager", return_value=manager),
        pytest.raises(ValueError, match="uses 8192 MiB"),
    ):
        _build_auto_config(vm_name="mac-test", os="macos", memory=4096, data_dir=tmp_path)


def test_open_desktop_uses_private_password_without_returning_it(tmp_path: Path) -> None:
    vm = _facade(_info(tmp_path))

    with patch("smolvm.macos.desktop.open_desktop") as opener:
        endpoint = vm.open_desktop()

    assert endpoint.viewer_url == "vnc://127.0.0.1:5901"
    opener.assert_called_once_with(endpoint, password="secret")


def test_open_desktop_password_read_error_names_recovery(tmp_path: Path) -> None:
    vm = _facade(_info(tmp_path))
    assert vm.info.config.macos_machine is not None
    (vm.info.config.macos_machine.bundle_path / ".smolvm-vnc-password").unlink()

    with pytest.raises(SmolVMError, match="smolvm sandbox desktop mac-test"):
        vm.open_desktop()


def test_open_desktop_stopped_error_names_recovery(tmp_path: Path) -> None:
    vm = _facade(_info(tmp_path, status=VMState.STOPPED))

    with pytest.raises(SmolVMError, match="smolvm sandbox start mac-test"):
        vm.open_desktop()


@pytest.mark.asyncio
async def test_async_start_skips_ssh_workspace_flow_for_macos(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    created = _info(tmp_path, status=VMState.CREATED)
    config = created.config.model_copy(
        update={"workspace_mounts": [WorkspaceMount(host_path=shared)]}
    )
    created = created.model_copy(update={"config": config})
    running = created.model_copy(
        update={"status": VMState.RUNNING, "display": DesktopEndpoint(port=5901)}
    )
    vm = _facade(created)
    vm._sdk.async_start = AsyncMock(return_value=running)

    with (
        patch.object(vm, "_reset_runtime_state"),
        patch.object(vm, "can_run_commands") as can_run,
        patch.object(vm, "_mount_workspaces") as mount_workspaces,
    ):
        await vm.async_start()

    can_run.assert_not_called()
    mount_workspaces.assert_not_called()
