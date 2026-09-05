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

"""Regression tests for per-VM disks outliving their sandbox.

A disk under ``data_dir/disks`` is only reachable through its inventory row.
Once the row is gone the file cannot be found by name again, so every path
that drops a row has to take the disk with it.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from smolvm.exceptions import SmolVMError, VMNotFoundError
from smolvm.types import NetworkConfig, VMConfig
from smolvm.vm import SmolVMManager


@pytest.fixture
def manager(tmp_path: Path) -> SmolVMManager:
    """A manager with temporary directories and a stubbed host network."""
    vm_manager = SmolVMManager(
        data_dir=tmp_path / "data",
        socket_dir=tmp_path / "sockets",
        backend="firecracker",
    )
    network = MagicMock()
    network.host_ip = "172.16.0.1"
    network.generate_mac.return_value = "AA:FC:00:00:00:01"
    network.async_prepare_tap_device = AsyncMock()
    network.async_add_route = AsyncMock()
    network.async_setup_nat = AsyncMock()
    network.async_setup_ssh_port_forward = AsyncMock()
    network.async_cleanup_nat_rules = AsyncMock()
    network.async_cleanup_tap = AsyncMock()
    network.async_cleanup_ssh_port_forward = AsyncMock()
    network.async_cleanup_all_local_port_forwards = AsyncMock()
    network.async_remove_egress_rules = AsyncMock()
    vm_manager.network = network
    return vm_manager


@pytest.fixture
def config(tmp_path: Path) -> VMConfig:
    """A minimal config whose rootfs is cloned into an isolated per-VM disk."""
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.write_bytes(b"rootfs")
    return VMConfig(vm_id="sbx-disk", kernel_path=kernel, rootfs_path=rootfs)


def _disk(manager: SmolVMManager, vm_id: str = "sbx-disk") -> Path:
    return manager.data_dir / "disks" / f"{vm_id}.ext4"


class TestFailedTeardownKeepsNoDisk:
    """A host-teardown failure must not cost the caller their disk."""

    def test_delete_removes_disk_when_a_teardown_step_fails(
        self,
        manager: SmolVMManager,
        config: VMConfig,
    ) -> None:
        """delete() drops the row either way, so the disk has to go too."""
        manager.create(config)
        assert _disk(manager).exists()

        manager.state.release_ssh_port = MagicMock(  # type: ignore[method-assign]
            side_effect=OSError("host teardown failed")
        )

        manager.delete("sbx-disk")

        assert not _disk(manager).exists()
        assert manager.list_vms() == []

    @pytest.mark.asyncio
    async def test_async_delete_removes_disk_when_a_teardown_step_fails(
        self,
        manager: SmolVMManager,
        config: VMConfig,
    ) -> None:
        """The async path drops the row the same way, so it must clean up too."""
        await manager.async_create(config)
        assert _disk(manager).exists()

        manager.state.release_ssh_port = MagicMock(  # type: ignore[method-assign]
            side_effect=OSError("host teardown failed")
        )

        await manager.async_delete("sbx-disk")

        assert not _disk(manager).exists()
        assert manager.list_vms() == []

    def test_failed_network_teardown_still_releases_reservations(
        self,
        manager: SmolVMManager,
        config: VMConfig,
    ) -> None:
        """Leases and ports outlive the row too, so every release must run."""
        manager.create(config)
        # Pin the reservations rather than relying on which ones a given host
        # hands out: what matters is that a failure does not skip them.
        lease = manager.state.get_ip_lease("sbx-disk")
        if lease is None:
            manager.state.allocate_ip("sbx-disk", "tap-test")
            lease = manager.state.get_ip_lease("sbx-disk")
        assert lease is not None
        host_port = manager.state.get_ssh_port("sbx-disk") or manager.state.reserve_ssh_port(
            "sbx-disk"
        )
        manager.state.update_vm(
            "sbx-disk",
            network=NetworkConfig(
                guest_ip=lease[0],
                gateway_ip="172.16.0.1",
                tap_device=lease[1],
                guest_mac="AA:FC:00:00:00:01",
                ssh_host_port=host_port,
            ),
        )
        manager.network.cleanup_nat_rules.side_effect = SmolVMError("no outbound interface")

        manager.delete("sbx-disk")

        manager.network.cleanup_nat_rules.assert_called_once()
        assert manager.state.get_ssh_port("sbx-disk") is None
        assert manager.state.get_ip_lease("sbx-disk") is None
        assert not _disk(manager).exists()

    def test_create_rollback_removes_disk_when_cleanup_fails(
        self,
        manager: SmolVMManager,
        config: VMConfig,
    ) -> None:
        """A create that fails at TAP setup must not leave a full disk copy."""
        manager.network.prepare_tap_device.side_effect = SmolVMError("no sudo")
        manager.network.cleanup_nat_rules.side_effect = SmolVMError("no outbound interface")

        with pytest.raises(SmolVMError, match="no sudo"):
            manager.create(config)

        assert not _disk(manager).exists()
        assert manager.list_vms() == []


class TestInterruptedCreate:
    """Ctrl-C lands between writing the disk and writing the row."""

    def test_keyboard_interrupt_while_materializing_leaves_no_disk(
        self,
        manager: SmolVMManager,
        config: VMConfig,
    ) -> None:
        """KeyboardInterrupt is a BaseException; the rollback must still run."""

        def interrupt(*_args: object, **_kwargs: object) -> None:
            _disk(manager).write_bytes(b"partial copy")
            raise KeyboardInterrupt

        with (
            patch.object(SmolVMManager, "_copy_with_reflink", side_effect=interrupt),
            pytest.raises(KeyboardInterrupt),
        ):
            manager.create(config)

        assert not _disk(manager).exists()

    @pytest.mark.asyncio
    async def test_keyboard_interrupt_while_materializing_async_leaves_no_disk(
        self,
        manager: SmolVMManager,
        config: VMConfig,
    ) -> None:
        """Same guarantee for async_create()."""

        async def interrupt(*_args: object, **_kwargs: object) -> None:
            _disk(manager).write_bytes(b"partial copy")
            raise KeyboardInterrupt

        with (
            patch.object(SmolVMManager, "_async_copy_with_reflink", side_effect=interrupt),
            pytest.raises(KeyboardInterrupt),
        ):
            await manager.async_create(config)

        assert not _disk(manager).exists()


class TestReclaimingDisksWithoutRows:
    """A disk whose row is already gone still has to be reachable."""

    def test_delete_reclaims_a_disk_whose_row_is_missing(
        self,
        manager: SmolVMManager,
        config: VMConfig,
    ) -> None:
        """Deleting by name reclaims files an older SmolVM stranded."""
        manager.create(config)
        disk = _disk(manager)
        log = manager.data_dir / "sbx-disk.log"
        log.write_text("boot log")
        manager.state.delete_vm("sbx-disk")

        with pytest.raises(VMNotFoundError):
            manager.delete("sbx-disk")

        assert not disk.exists()
        assert not log.exists()

    def test_delete_keeps_a_saved_disk_whose_row_is_missing(
        self,
        manager: SmolVMManager,
        config: VMConfig,
    ) -> None:
        """A disk the user asked SmolVM to keep survives a later delete."""
        saved = config.model_copy(update={"retain_disk_on_delete": True})
        manager.create(saved)
        disk = _disk(manager)
        manager.delete("sbx-disk")
        assert disk.exists()

        with pytest.raises(VMNotFoundError):
            manager.delete("sbx-disk")

        assert disk.exists()

    def test_saved_disk_is_marked_and_the_mark_clears_on_reuse(
        self,
        manager: SmolVMManager,
        config: VMConfig,
    ) -> None:
        """The marker is what tells a kept disk apart from a leaked one."""
        saved = config.model_copy(update={"retain_disk_on_delete": True})
        manager.create(saved)
        disk = _disk(manager)
        marker = disk.with_name(f"{disk.name}.retained")

        manager.delete("sbx-disk")
        assert marker.exists()

        manager.create(saved)
        assert disk.exists()
        assert not marker.exists()

    def test_failed_create_leaves_a_saved_disk_still_marked(
        self,
        manager: SmolVMManager,
        config: VMConfig,
    ) -> None:
        """A create that reuses a saved disk and then fails must not unmark it."""
        saved = config.model_copy(update={"retain_disk_on_delete": True})
        manager.create(saved)
        disk = _disk(manager)
        marker = disk.with_name(f"{disk.name}.retained")
        manager.delete("sbx-disk")
        assert marker.exists()

        manager.network.prepare_tap_device.side_effect = SmolVMError("no sudo")
        with pytest.raises(SmolVMError, match="no sudo"):
            manager.create(config)

        assert disk.exists()
        assert marker.exists()
        assert manager.prune_leftover_artifacts() == []

    def test_delete_removes_the_runtime_log(
        self,
        manager: SmolVMManager,
        config: VMConfig,
    ) -> None:
        """Logs are per-VM and nothing reads them once the VM is gone."""
        manager.create(config)
        log = manager.data_dir / "sbx-disk.log"
        log.write_text("boot log")

        manager.delete("sbx-disk")

        assert not log.exists()


class TestLeftoverSweep:
    """``smolvm sandbox prune`` needs an accurate view of what is reclaimable."""

    def test_reports_leftovers_and_leaves_live_sandboxes_alone(
        self,
        manager: SmolVMManager,
        config: VMConfig,
    ) -> None:
        """Only files whose sandbox is gone count as leftovers."""
        manager.create(config)
        manager.create(config.model_copy(update={"vm_id": "sbx-live"}))
        (manager.data_dir / "sbx-disk.log").write_text("boot log")
        manager.state.delete_vm("sbx-disk")

        found = {(item.vm_id, item.path.name) for item in manager.find_leftover_artifacts()}

        assert found == {("sbx-disk", "sbx-disk.ext4"), ("sbx-disk", "sbx-disk.log")}

    def test_skips_saved_disks_unless_asked(
        self,
        manager: SmolVMManager,
        config: VMConfig,
    ) -> None:
        """Deleting state the user asked to keep needs an explicit opt-in."""
        saved = config.model_copy(update={"retain_disk_on_delete": True})
        manager.create(saved)
        manager.delete("sbx-disk")
        disk = _disk(manager)

        assert manager.prune_leftover_artifacts() == []
        assert disk.exists()

        removed = manager.prune_leftover_artifacts(include_retained=True)

        assert {item.path.name for item in removed} == {
            "sbx-disk.ext4",
            "sbx-disk.ext4.retained",
        }
        assert not disk.exists()

    def test_dry_run_reports_without_deleting(
        self,
        manager: SmolVMManager,
        config: VMConfig,
    ) -> None:
        """A dry run has to be safe to run on a production data directory."""
        manager.create(config)
        manager.state.delete_vm("sbx-disk")

        planned = manager.prune_leftover_artifacts(dry_run=True)

        assert [item.path.name for item in planned] == ["sbx-disk.ext4"]
        assert _disk(manager).exists()

    def test_skips_a_disk_a_running_process_is_using(
        self,
        manager: SmolVMManager,
        config: VMConfig,
    ) -> None:
        """Sandboxes started from an SDK script are not in the CLI inventory."""
        manager.create(config)
        manager.state.delete_vm("sbx-disk")

        with patch.object(
            SmolVMManager,
            "_running_process_args",
            return_value=f"qemu-system-aarch64 -drive file={_disk(manager)},if=virtio",
        ):
            assert manager.find_leftover_artifacts() == []

        assert _disk(manager).exists()

    def test_refuses_to_sweep_when_running_sandboxes_cannot_be_listed(
        self,
        manager: SmolVMManager,
        config: VMConfig,
    ) -> None:
        """Without that check the sweep could delete a live sandbox's disk."""
        manager.create(config)
        manager.state.delete_vm("sbx-disk")

        with (
            patch.object(SmolVMManager, "_running_process_args", return_value=None),
            pytest.raises(SmolVMError, match="Cannot check which sandboxes are running"),
        ):
            manager.prune_leftover_artifacts()

        assert _disk(manager).exists()


class TestStrictBridgeCleanup:
    """The one failure that must still abort a delete."""

    def test_failed_bridge_teardown_keeps_the_row_and_the_disk(
        self,
        manager: SmolVMManager,
        config: VMConfig,
    ) -> None:
        """Both have to survive so a later delete can retry the teardown."""
        manager.create(config)
        manager.state.get_ip_lease = MagicMock(  # type: ignore[method-assign]
            side_effect=OSError("bridge teardown failed")
        )

        with pytest.raises(OSError, match="bridge teardown failed"):
            manager._cleanup_resources("sbx-disk", require_bridge_cleanup=True)

        assert _disk(manager).exists()
