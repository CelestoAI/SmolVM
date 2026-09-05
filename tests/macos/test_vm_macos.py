# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.exceptions import SmolVMError
from smolvm.runtime.base import RuntimeLaunch
from smolvm.types import (
    DesktopEndpoint,
    GuestOS,
    MacOSMachineConfig,
    VMConfig,
    VMInfo,
    VMState,
    WorkspaceMount,
)
from smolvm.vm import SmolVMManager


def _config(tmp_path: Path) -> VMConfig:
    shared = tmp_path / "shared"
    shared.mkdir()
    return VMConfig(
        vm_id="mac-test",
        guest_os=GuestOS.MACOS,
        backend="vz",
        boot_mode="platform",
        memory=8192,
        macos_machine=MacOSMachineConfig(
            base_image="macos-latest",
            manifest_path=tmp_path / "manifest.json",
            bundle_path=tmp_path / "data" / "macos-vms" / "mac-test",
            guest_version="26.0",
        ),
        workspace_mounts=[WorkspaceMount(host_path=shared)],
    )


def test_manager_create_macos_skips_linux_disk_and_network(tmp_path: Path) -> None:
    manager = SmolVMManager(
        data_dir=tmp_path / "data",
        socket_dir=tmp_path / "sockets",
        backend="vz",
    )
    config = _config(tmp_path)

    with (
        patch.object(manager, "_materialize_rootfs") as materialize,
        patch.object(manager, "_materialize_macos_bundle") as materialize_macos,
    ):
        info = manager.create(config)

    materialize.assert_not_called()
    materialize_macos.assert_called_once_with(config)
    assert info.status is VMState.CREATED
    assert info.network is None
    assert info.config.macos_machine == config.macos_machine


def test_manager_rejects_unmanaged_macos_parent_before_creating_it(tmp_path: Path) -> None:
    manager = SmolVMManager(
        data_dir=tmp_path / "data",
        socket_dir=tmp_path / "sockets",
        backend="vz",
    )
    config = _config(tmp_path)
    assert config.macos_machine is not None
    outside = tmp_path / "outside"
    config = config.model_copy(
        update={
            "macos_machine": config.macos_machine.model_copy(
                update={"bundle_path": outside / "mac-test"}
            )
        }
    )

    with pytest.raises(SmolVMError, match="must stay"):
        manager._materialize_macos_bundle(config)

    assert not outside.exists()


def test_manager_materializes_macos_bundle_with_driver_clone(tmp_path: Path) -> None:
    manager = SmolVMManager(
        data_dir=tmp_path / "data",
        socket_dir=tmp_path / "sockets",
        backend="vz",
    )
    config = _config(tmp_path)
    driver = MagicMock()

    def create_bundle(*args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        assert kwargs["destination_storage"] == config.macos_machine.bundle_path.parent
        config.macos_machine.bundle_path.mkdir(parents=True)

    driver.clone.side_effect = create_bundle
    with (
        patch("smolvm.host.lume.find_lume_binary", return_value=Path("/tmp/lume")),
        patch("smolvm.host.lume.pinned_lume_ready", return_value=True),
        patch("smolvm.macos.lume.LumeDriver", return_value=driver),
    ):
        manager._materialize_macos_bundle(config)

    driver.clone.assert_called_once()
    assert config.macos_machine.bundle_path.is_dir()
    assert config.macos_machine.bundle_path.stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_manager_async_create_macos_skips_linux_network(tmp_path: Path) -> None:
    manager = SmolVMManager(
        data_dir=tmp_path / "data",
        socket_dir=tmp_path / "sockets",
        backend="vz",
    )
    config = _config(tmp_path)

    with patch.object(manager, "_materialize_macos_bundle") as materialize:
        info = await manager.async_create(config)

    materialize.assert_called_once_with(config)
    assert info.status is VMState.CREATED
    assert info.network is None


def test_manager_start_and_stop_persist_macos_desktop(tmp_path: Path) -> None:
    manager = SmolVMManager(
        data_dir=tmp_path / "data",
        socket_dir=tmp_path / "sockets",
        backend="vz",
    )
    with patch.object(manager, "_materialize_macos_bundle"):
        manager.create(_config(tmp_path))
    endpoint = DesktopEndpoint(port=5901, width=1440, height=900)
    adapter = MagicMock()
    adapter.start.return_value = RuntimeLaunch(
        pid=12345,
        control_socket_path=None,
        status=VMState.RUNNING,
        display=endpoint,
    )

    with patch.object(manager, "_runtime_adapter_for_backend", return_value=adapter):
        running = manager.start("mac-test")
        stopped = manager.stop("mac-test")

    assert running.status is VMState.RUNNING
    assert running.display == endpoint
    assert stopped.status is VMState.STOPPED
    assert stopped.display is None
    adapter.stop.assert_called_once()


def test_manager_refuses_third_running_macos_guest(tmp_path: Path) -> None:
    manager = SmolVMManager(
        data_dir=tmp_path / "data",
        socket_dir=tmp_path / "sockets",
        backend="vz",
    )
    base = _config(tmp_path)
    assert base.macos_machine is not None
    for name in ("mac-one", "mac-two", "mac-three"):
        config = base.model_copy(
            update={
                "vm_id": name,
                "macos_machine": base.macos_machine.model_copy(
                    update={"bundle_path": tmp_path / name}
                ),
            }
        )
        manager.state.create_vm(config)
    manager.state.update_vm("mac-one", status=VMState.RUNNING, pid=1)
    manager.state.update_vm("mac-two", status=VMState.RUNNING, pid=2)

    with pytest.raises(SmolVMError, match="sandbox stop mac-one"):
        manager.start("mac-three")


def test_managed_disk_helper_skips_macos_without_rootfs(tmp_path: Path) -> None:
    manager = SmolVMManager(
        data_dir=tmp_path / "data",
        socket_dir=tmp_path / "sockets",
        backend="vz",
    )
    info = VMInfo(vm_id="mac-test", status=VMState.CREATED, config=_config(tmp_path))

    assert manager._managed_disk_for_vm(info) is None


def test_manager_delete_removes_macos_bundle(tmp_path: Path) -> None:
    manager = SmolVMManager(
        data_dir=tmp_path / "data",
        socket_dir=tmp_path / "sockets",
        backend="vz",
    )
    config = _config(tmp_path)
    assert config.macos_machine is not None
    bundle = tmp_path / "data" / "macos-vms" / "mac-test"
    config = config.model_copy(
        update={"macos_machine": config.macos_machine.model_copy(update={"bundle_path": bundle})}
    )
    bundle.mkdir(parents=True)
    manager.state.create_vm(config)
    driver = MagicMock()
    driver.delete.side_effect = lambda *args, **kwargs: bundle.rmdir()

    with (
        patch("smolvm.host.lume.find_lume_binary", return_value=Path("/tmp/lume")),
        patch("smolvm.host.lume.pinned_lume_ready", return_value=True),
        patch("smolvm.macos.lume.LumeDriver", return_value=driver),
    ):
        manager.delete("mac-test")

    driver.delete.assert_called_once()
    assert not config.macos_machine.bundle_path.exists()
