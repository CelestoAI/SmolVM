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

"""Tests for SmolVM storage module."""

from pathlib import Path

import pytest

from smolvm.exceptions import VMAlreadyExistsError, VMNotFoundError
from smolvm.storage import StateManager
from smolvm.types import NetworkConfig, VMConfig, VMState


@pytest.fixture
def state_manager(tmp_path: Path) -> StateManager:
    """Create a StateManager with a temporary database."""
    return StateManager(tmp_path / "test.db")


@pytest.fixture
def sample_config(tmp_path: Path) -> VMConfig:
    """Create a sample VMConfig."""
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()

    return VMConfig(
        vm_id="vm001",
        vcpu_count=2,
        mem_size_mib=512,
        kernel_path=kernel,
        rootfs_path=rootfs,
    )


class TestStateManagerVMOperations:
    """Tests for VM CRUD operations."""

    def test_create_vm(self, state_manager: StateManager, sample_config: VMConfig) -> None:
        """Test creating a VM."""
        vm_info = state_manager.create_vm(sample_config)

        assert vm_info.vm_id == "vm001"
        assert vm_info.status == VMState.CREATED
        assert vm_info.config == sample_config

    def test_create_duplicate_vm_raises(
        self, state_manager: StateManager, sample_config: VMConfig
    ) -> None:
        """Test that creating a duplicate VM raises an error."""
        state_manager.create_vm(sample_config)

        with pytest.raises(VMAlreadyExistsError) as exc_info:
            state_manager.create_vm(sample_config)

        assert exc_info.value.vm_id == "vm001"

    def test_get_vm(self, state_manager: StateManager, sample_config: VMConfig) -> None:
        """Test getting a VM."""
        state_manager.create_vm(sample_config)

        vm_info = state_manager.get_vm("vm001")

        assert vm_info.vm_id == "vm001"
        assert vm_info.status == VMState.CREATED

    def test_get_nonexistent_vm_raises(self, state_manager: StateManager) -> None:
        """Test that getting a nonexistent VM raises an error."""
        with pytest.raises(VMNotFoundError) as exc_info:
            state_manager.get_vm("nonexistent")

        assert exc_info.value.vm_id == "nonexistent"

    def test_update_vm_status(self, state_manager: StateManager, sample_config: VMConfig) -> None:
        """Test updating VM status."""
        state_manager.create_vm(sample_config)

        vm_info = state_manager.update_vm("vm001", status=VMState.RUNNING, pid=12345)

        assert vm_info.status == VMState.RUNNING
        assert vm_info.pid == 12345

    def test_update_vm_network(self, state_manager: StateManager, sample_config: VMConfig) -> None:
        """Test updating VM network configuration."""
        state_manager.create_vm(sample_config)

        network = NetworkConfig(
            guest_ip="172.16.0.2",
            tap_device="tap1",
            guest_mac="AA:FC:00:00:00:01",
        )
        vm_info = state_manager.update_vm("vm001", network=network)

        assert vm_info.network is not None
        assert vm_info.network.guest_ip == "172.16.0.2"

    def test_delete_vm(self, state_manager: StateManager, sample_config: VMConfig) -> None:
        """Test deleting a VM."""
        state_manager.create_vm(sample_config)
        state_manager.delete_vm("vm001")

        with pytest.raises(VMNotFoundError):
            state_manager.get_vm("vm001")

    def test_list_vms(self, state_manager: StateManager, tmp_path: Path) -> None:
        """Test listing VMs."""
        # Create multiple VMs
        for i in range(3):
            kernel = tmp_path / f"vmlinux{i}"
            rootfs = tmp_path / f"rootfs{i}.ext4"
            kernel.touch()
            rootfs.touch()

            config = VMConfig(
                vm_id=f"vm{i:03d}",
                kernel_path=kernel,
                rootfs_path=rootfs,
            )
            state_manager.create_vm(config)

        vms = state_manager.list_vms()
        assert len(vms) == 3

    def test_list_vms_by_status(self, state_manager: StateManager, sample_config: VMConfig) -> None:
        """Test listing VMs filtered by status."""
        state_manager.create_vm(sample_config)
        state_manager.update_vm("vm001", status=VMState.RUNNING)

        running = state_manager.list_vms(status=VMState.RUNNING)
        stopped = state_manager.list_vms(status=VMState.STOPPED)

        assert len(running) == 1
        assert len(stopped) == 0


class TestIPAllocation:
    """Tests for IP allocation."""

    def test_allocate_ip(self, state_manager: StateManager, sample_config: VMConfig) -> None:
        """Test allocating an IP address."""
        state_manager.create_vm(sample_config)

        ip = state_manager.allocate_ip("vm001", "tap1")

        assert ip == "172.16.0.2"

    def test_allocate_sequential_ips(self, state_manager: StateManager, tmp_path: Path) -> None:
        """Test that IPs are allocated sequentially."""
        allocated = []

        for i in range(5):
            kernel = tmp_path / f"vmlinux{i}"
            rootfs = tmp_path / f"rootfs{i}.ext4"
            kernel.touch()
            rootfs.touch()

            config = VMConfig(
                vm_id=f"vm{i:03d}",
                kernel_path=kernel,
                rootfs_path=rootfs,
            )
            state_manager.create_vm(config)
            ip = state_manager.allocate_ip(f"vm{i:03d}", f"tap{i}")
            allocated.append(ip)

        assert allocated == [
            "172.16.0.2",
            "172.16.0.3",
            "172.16.0.4",
            "172.16.0.5",
            "172.16.0.6",
        ]

    def test_release_ip(self, state_manager: StateManager, sample_config: VMConfig) -> None:
        """Test releasing an IP address."""
        state_manager.create_vm(sample_config)
        state_manager.allocate_ip("vm001", "tap1")

        state_manager.release_ip("vm001")

        lease = state_manager.get_ip_lease("vm001")
        assert lease is None

    def test_ip_reuse_after_release(self, state_manager: StateManager, tmp_path: Path) -> None:
        """Test that released IPs can be reused."""
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        config1 = VMConfig(vm_id="vm001", kernel_path=kernel, rootfs_path=rootfs)
        config2 = VMConfig(vm_id="vm002", kernel_path=kernel, rootfs_path=rootfs)

        state_manager.create_vm(config1)
        ip1 = state_manager.allocate_ip("vm001", "tap1")
        assert ip1 == "172.16.0.2"

        state_manager.release_ip("vm001")

        state_manager.create_vm(config2)
        ip2 = state_manager.allocate_ip("vm002", "tap2")
        assert ip2 == "172.16.0.2"  # Reused!

    def test_update_ip_lease_tap(
        self, state_manager: StateManager, sample_config: VMConfig
    ) -> None:
        """Test updating the TAP device name for a lease."""
        state_manager.create_vm(sample_config)
        state_manager.allocate_ip("vm001", "pending")

        state_manager.update_ip_lease_tap("vm001", "tap2")

        lease = state_manager.get_ip_lease("vm001")
        assert lease is not None
        assert lease[1] == "tap2"


class TestSSHPortAllocation:
    """Tests for SSH host-port reservation."""

    def test_reserve_ssh_port(self, state_manager: StateManager, sample_config: VMConfig) -> None:
        """Test reserving an SSH host port."""
        state_manager.create_vm(sample_config)

        port = state_manager.reserve_ssh_port("vm001")

        assert port == 2200
        assert state_manager.get_ssh_port("vm001") == 2200

    def test_reserve_ssh_port_is_stable(
        self, state_manager: StateManager, sample_config: VMConfig
    ) -> None:
        """Test reserving twice returns the same host port."""
        state_manager.create_vm(sample_config)

        p1 = state_manager.reserve_ssh_port("vm001")
        p2 = state_manager.reserve_ssh_port("vm001")

        assert p1 == p2

    def test_reserve_ssh_ports_sequential(
        self,
        state_manager: StateManager,
        tmp_path: Path,
    ) -> None:
        """Test that SSH ports are allocated sequentially."""
        ports = []
        for i in range(3):
            kernel = tmp_path / f"vmlinux-ssh-{i}"
            rootfs = tmp_path / f"rootfs-ssh-{i}.ext4"
            kernel.touch()
            rootfs.touch()
            config = VMConfig(
                vm_id=f"vm-ssh-{i}",
                kernel_path=kernel,
                rootfs_path=rootfs,
            )
            state_manager.create_vm(config)
            ports.append(state_manager.reserve_ssh_port(f"vm-ssh-{i}"))

        assert ports == [2200, 2201, 2202]

    def test_release_ssh_port(self, state_manager: StateManager, sample_config: VMConfig) -> None:
        """Test releasing a reserved SSH host port."""
        state_manager.create_vm(sample_config)
        state_manager.reserve_ssh_port("vm001")
        state_manager.release_ssh_port("vm001")

        assert state_manager.get_ssh_port("vm001") is None

    def test_reuse_released_ssh_port(self, state_manager: StateManager, tmp_path: Path) -> None:
        """Test that released SSH ports are reused."""
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        config1 = VMConfig(vm_id="vm001", kernel_path=kernel, rootfs_path=rootfs)
        config2 = VMConfig(vm_id="vm002", kernel_path=kernel, rootfs_path=rootfs)

        state_manager.create_vm(config1)
        p1 = state_manager.reserve_ssh_port("vm001")
        assert p1 == 2200
        state_manager.release_ssh_port("vm001")

        state_manager.create_vm(config2)
        p2 = state_manager.reserve_ssh_port("vm002")
        assert p2 == 2200


class TestReconciliation:
    """Tests for state reconciliation."""

    def test_reconcile_dead_process(
        self,
        state_manager: StateManager,
        sample_config: VMConfig,
    ) -> None:
        """Test that dead processes are detected."""
        state_manager.create_vm(sample_config)
        # Simulate a running VM with a non-existent PID
        state_manager.update_vm(
            "vm001",
            status=VMState.RUNNING,
            pid=999999,  # Very unlikely to exist
        )

        stale = state_manager.reconcile()

        assert "vm001" in stale

        # Verify it was marked as ERROR
        vm_info = state_manager.get_vm("vm001")
        assert vm_info.status == VMState.ERROR
        assert vm_info.pid is None
