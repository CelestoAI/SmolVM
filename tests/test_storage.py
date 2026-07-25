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

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from smolvm.cli.state import SQLiteStateManager as StateManager
from smolvm.exceptions import (
    BrowserSessionNotFoundError,
    SnapshotNotFoundError,
    VMAlreadyExistsError,
    VMNotFoundError,
)
from smolvm.storage import SSH_PORT_END
from smolvm.types import (
    BrowserSessionConfig,
    BrowserSessionInfo,
    BrowserSessionState,
    DesktopEndpoint,
    NetworkConfig,
    PortForwardConfig,
    SnapshotArtifacts,
    SnapshotInfo,
    VMConfig,
    VMState,
)


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
        memory=512,
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

    def test_get_vm_allows_missing_persisted_paths(
        self,
        state_manager: StateManager,
        sample_config: VMConfig,
    ) -> None:
        """Persisted VM metadata should remain readable after cache cleanup."""
        state_manager.create_vm(sample_config)
        sample_config.kernel_path.unlink()

        vm_info = state_manager.get_vm("vm001")

        assert vm_info.vm_id == "vm001"
        assert vm_info.config.kernel_path == sample_config.kernel_path

    def test_get_vm_allows_missing_persisted_initrd(
        self,
        state_manager: StateManager,
        tmp_path: Path,
    ) -> None:
        """Persisted VM metadata should tolerate a cleaned-up initrd cache."""
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        initrd = tmp_path / "initrd"
        kernel.touch()
        rootfs.touch()
        initrd.touch()

        config = VMConfig(
            vm_id="vm001",
            kernel_path=kernel,
            initrd_path=initrd,
            rootfs_path=rootfs,
        )
        state_manager.create_vm(config)
        initrd.unlink()

        vm_info = state_manager.get_vm("vm001")

        assert vm_info.vm_id == "vm001"
        assert vm_info.config.initrd_path == initrd

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

    def test_update_and_clear_vm_desktop(
        self, state_manager: StateManager, sample_config: VMConfig
    ) -> None:
        state_manager.create_vm(sample_config)
        endpoint = DesktopEndpoint(port=5901, width=1440, height=900)

        updated = state_manager.update_vm("vm001", display=endpoint)
        assert updated.display == endpoint
        assert state_manager.list_vms()[0].display == endpoint

        cleared = state_manager.update_vm("vm001", clear_display=True)
        assert cleared.display is None

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

    def test_list_vms_allows_missing_persisted_paths(
        self,
        state_manager: StateManager,
        tmp_path: Path,
    ) -> None:
        """Listing should tolerate stale image paths in persisted VM configs."""
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        state_manager.create_vm(
            VMConfig(
                vm_id="vm001",
                kernel_path=kernel,
                rootfs_path=rootfs,
            )
        )
        kernel.unlink()

        vms = state_manager.list_vms()

        assert [vm.vm_id for vm in vms] == ["vm001"]
        assert vms[0].config.kernel_path == kernel

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

    def test_ssh_port_pool_ends_at_tcp_max_port(self) -> None:
        """The SSH port pool must not allocate invalid TCP ports."""
        assert SSH_PORT_END == 65535

    @pytest.mark.parametrize("host_port", [0, -1, 65536])
    def test_requested_host_port_must_be_a_valid_tcp_port(
        self,
        state_manager: StateManager,
        sample_config: VMConfig,
        host_port: int,
    ) -> None:
        """An unusable requested port is rejected up front.

        ``guest_port`` was range-checked but ``host_port`` was not, so an
        out-of-range value was persisted and only surfaced much later as an
        opaque hypervisor error. Port 0 was worse: it means "any port", so the
        number recorded in state did not match what the VM actually listened on.
        """
        state_manager.create_vm(sample_config)

        with pytest.raises(ValueError, match="host_port must be 1-65535"):
            state_manager.reserve_ssh_port("vm001", host_port=host_port)

        assert state_manager.get_ssh_port("vm001") is None

    def test_valid_requested_host_port_is_reserved(
        self, state_manager: StateManager, sample_config: VMConfig
    ) -> None:
        """The range check must not reject legitimate ports."""
        state_manager.create_vm(sample_config)

        assert state_manager.reserve_ssh_port("vm001", host_port=65535) == 65535

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

    def test_reserve_ssh_port_skips_excluded_ports(
        self,
        state_manager: StateManager,
        sample_config: VMConfig,
    ) -> None:
        """Port reservation can skip ports that are busy outside SmolVM state."""
        state_manager.create_vm(sample_config)

        port = state_manager.reserve_ssh_port("vm001", excluded_host_ports={2200, 2201})

        assert port == 2202
        assert state_manager.get_ssh_port("vm001") == 2202

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


class TestSnapshotStorage:
    """Tests for snapshot persistence."""

    def test_create_get_list_and_delete_snapshot(
        self,
        state_manager: StateManager,
        sample_config: VMConfig,
        tmp_path: Path,
    ) -> None:
        """Snapshot metadata should round-trip through SQLite."""
        config_with_forwards = sample_config.model_copy(
            update={
                "port_forwards": [
                    PortForwardConfig(host_port=39011, guest_port=9222),
                ]
            }
        )
        state_manager.create_vm(config_with_forwards)
        network = NetworkConfig(
            guest_ip="172.16.0.2",
            tap_device="tap2",
            guest_mac="AA:FC:00:00:00:02",
            ssh_host_port=2200,
        )
        state_manager.update_vm("vm001", network=network)

        snapshot_dir = tmp_path / "snapshots" / "snap-001"
        snapshot_dir.mkdir(parents=True)
        snapshot = SnapshotInfo(
            snapshot_id="snap-001",
            vm_id="vm001",
            backend="firecracker",
            artifacts=SnapshotArtifacts(
                state_path=snapshot_dir / "vmstate.bin",
                memory_path=snapshot_dir / "mem.bin",
                disk_path=snapshot_dir / "disk.ext4",
            ),
            vm_config=config_with_forwards,
            network_config=network,
            created_at=datetime.now(timezone.utc),
        )
        assert snapshot.artifacts.state_path is not None
        assert snapshot.artifacts.memory_path is not None
        snapshot.artifacts.state_path.write_bytes(b"")
        snapshot.artifacts.memory_path.write_bytes(b"")
        snapshot.artifacts.disk_path.write_bytes(b"")

        created = state_manager.create_snapshot(snapshot)
        fetched = state_manager.get_snapshot("snap-001")
        listed = state_manager.list_snapshots(vm_id="vm001")

        assert created.snapshot_id == "snap-001"
        assert fetched.backend == "firecracker"
        assert fetched.vm_config == config_with_forwards
        assert fetched.vm_config.rootfs_path == sample_config.rootfs_path
        assert isinstance(fetched.vm_config.port_forwards[0], PortForwardConfig)
        assert fetched.network_config.tap_device == "tap2"
        assert fetched.artifact_kind is None
        assert fetched.virtual_size_bytes is None
        assert fetched.changed_bytes is None
        assert fetched.bitmap_granularity_bytes is None
        assert fetched.bitmap_name is None
        assert [item.snapshot_id for item in listed] == ["snap-001"]

        restored = state_manager.mark_snapshot_restored("snap-001", "vm001")
        assert restored.restored is True
        assert restored.restored_vm_id == "vm001"

        state_manager.delete_snapshot("snap-001")
        with pytest.raises(SnapshotNotFoundError):
            state_manager.get_snapshot("snap-001")

    def test_allocate_specific_ip_and_ssh_port(
        self,
        state_manager: StateManager,
        sample_config: VMConfig,
    ) -> None:
        """Restore flows should be able to rehydrate exact network resources."""
        state_manager.create_vm(sample_config)

        ip = state_manager.allocate_ip("vm001", "tap22", requested_ip="172.16.0.22")
        ssh_port = state_manager.reserve_ssh_port("vm001", host_port=2222)

        assert ip == "172.16.0.22"
        assert ssh_port == 2222

    def test_qemu_snapshot_round_trip_preserves_backend_and_optional_artifacts(
        self,
        state_manager: StateManager,
        sample_config: VMConfig,
        tmp_path: Path,
    ) -> None:
        """QEMU snapshots should not be rehydrated as Firecracker snapshots."""
        qemu_config = sample_config.model_copy(update={"backend": "qemu"})
        state_manager.create_vm(qemu_config)
        network = NetworkConfig(
            guest_ip="172.16.0.2",
            tap_device="tap2",
            guest_mac="AA:FC:00:00:00:02",
            ssh_host_port=2200,
        )
        state_manager.update_vm("vm001", network=network)

        snapshot_dir = tmp_path / "snapshots" / "snap-qemu"
        snapshot_dir.mkdir(parents=True)
        disk_path = snapshot_dir / "increment.qcow2"
        disk_path.write_bytes(b"")
        snapshot = SnapshotInfo(
            snapshot_id="snap-qemu",
            vm_id="vm001",
            backend="qemu",
            artifacts=SnapshotArtifacts(disk_path=disk_path),
            vm_config=qemu_config,
            network_config=network,
            created_at=datetime.now(timezone.utc),
            artifact_kind="incremental",
            virtual_size_bytes=10 * 1024 * 1024,
            changed_bytes=131072,
            bitmap_granularity_bytes=65536,
            bitmap_name="celesto-chain0",
        )

        state_manager.create_snapshot(snapshot)
        fetched = state_manager.get_snapshot("snap-qemu")

        assert fetched.backend == "qemu"
        assert fetched.artifacts.state_path is None
        assert fetched.artifacts.memory_path is None
        assert fetched.artifacts.disk_path == disk_path
        assert fetched.artifact_kind == "incremental"
        assert fetched.virtual_size_bytes == 10 * 1024 * 1024
        assert fetched.changed_bytes == 131072
        assert fetched.bitmap_granularity_bytes == 65536
        assert fetched.bitmap_name == "celesto-chain0"

        with sqlite3.connect(state_manager.db_path) as conn:
            conn.execute(
                """
                UPDATE snapshots
                SET virtual_size_bytes = ?, changed_bytes = ?,
                    bitmap_granularity_bytes = ?, bitmap_name = ?
                WHERE snapshot_id = ?
                """,
                ("invalid", -1, 0, sqlite3.Binary(b"not-text"), "snap-qemu"),
            )
        malformed = state_manager.get_snapshot("snap-qemu")
        assert malformed.virtual_size_bytes is None
        assert malformed.changed_bytes is None
        assert malformed.bitmap_granularity_bytes is None
        assert malformed.bitmap_name is None


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

    @pytest.mark.parametrize("pid", [0, -1])
    def test_reconcile_non_positive_pid(
        self,
        state_manager: StateManager,
        sample_config: VMConfig,
        pid: int,
    ) -> None:
        """A pid of 0 or -1 is not a live VM.

        ``os.kill(0, 0)`` probes our own process group and ``os.kill(-1, 0)``
        probes every process we may signal, so both return successfully and the
        sandbox stayed wedged in RUNNING forever instead of being reconciled.
        """
        state_manager.create_vm(sample_config)
        state_manager.update_vm("vm001", status=VMState.RUNNING, pid=pid)

        assert "vm001" in state_manager.reconcile()
        assert state_manager.get_vm("vm001").status == VMState.ERROR

    def test_reconcile_leaves_live_process_alone(
        self,
        state_manager: StateManager,
        sample_config: VMConfig,
    ) -> None:
        """A real, running pid must not be reconciled away."""
        state_manager.create_vm(sample_config)
        state_manager.update_vm("vm001", status=VMState.RUNNING, pid=os.getpid())

        assert state_manager.reconcile() == []
        assert state_manager.get_vm("vm001").status == VMState.RUNNING


class TestBrowserSessionStorage:
    """Tests for browser session persistence."""

    def test_create_get_and_update_browser_session(
        self,
        state_manager: StateManager,
    ) -> None:
        """Browser session records should round-trip through SQLite."""
        config = BrowserSessionConfig(
            session_id="browser-abc123",
            mode="live",
            profile_mode="persistent",
            profile_id="acct-1",
            record_video=True,
        )
        expires_at = datetime.now(timezone.utc)
        info = BrowserSessionInfo(
            session_id="browser-abc123",
            vm_id="browser-prof-acct-1-deadbeef",
            status=BrowserSessionState.CREATED,
            profile_id="acct-1",
            expires_at=expires_at,
            artifacts_dir=Path("/tmp/browser-abc123"),
        )

        created = state_manager.create_browser_session(info, config)
        fetched = state_manager.get_browser_session("browser-abc123")
        fetched_config = state_manager.get_browser_session_config("browser-abc123")

        assert created == info
        assert fetched == info
        assert fetched_config == config

        updated = state_manager.update_browser_session(
            "browser-abc123",
            status=BrowserSessionState.READY,
            cdp_url="http://127.0.0.1:39222",
            live_url="http://127.0.0.1:36080/vnc.html",
            vnc_url="vnc://127.0.0.1:35900",
            debug_port=39222,
            vnc_port=35900,
        )

        assert updated.status == BrowserSessionState.READY
        assert updated.cdp_url == "http://127.0.0.1:39222"
        assert updated.live_url == "http://127.0.0.1:36080/vnc.html"
        assert updated.vnc_url == "vnc://127.0.0.1:35900"
        assert updated.vnc_port == 35900

    def test_list_and_delete_browser_sessions(self, state_manager: StateManager) -> None:
        """Browser sessions should be listable and removable."""
        first = BrowserSessionInfo(
            session_id="browser-001",
            vm_id="browser-001",
            status=BrowserSessionState.CREATED,
        )
        second = BrowserSessionInfo(
            session_id="browser-002",
            vm_id="browser-002",
            status=BrowserSessionState.READY,
        )

        state_manager.create_browser_session(
            first,
            BrowserSessionConfig(session_id="browser-001"),
        )
        state_manager.create_browser_session(
            second,
            BrowserSessionConfig(session_id="browser-002"),
        )

        sessions = state_manager.list_browser_sessions()
        ready_only = state_manager.list_browser_sessions(status=BrowserSessionState.READY)

        assert len(sessions) == 2
        assert [session.session_id for session in ready_only] == ["browser-002"]

        state_manager.delete_browser_session("browser-001")
        with pytest.raises(BrowserSessionNotFoundError):
            state_manager.get_browser_session("browser-001")
