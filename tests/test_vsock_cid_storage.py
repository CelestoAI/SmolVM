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

"""Tests for per-VM vsock CID allocation (mirrors the SSH-port pool)."""

from pathlib import Path

import pytest

from smolvm.storage import StateManager
from smolvm.storage._base import VSOCK_CID_START
from smolvm.types import GuestOS, VMConfig


def _make_config(tmp_path: Path, vm_id: str) -> VMConfig:
    rootfs = tmp_path / f"{vm_id}.ext4"
    rootfs.write_bytes(b"rootfs")
    kernel = tmp_path / f"{vm_id}.bin"
    kernel.write_bytes(b"kernel")
    return VMConfig(
        vm_id=vm_id,
        kernel_path=kernel,
        rootfs_path=rootfs,
        guest_os=GuestOS.ALPINE,
    )


@pytest.fixture
def state(tmp_path: Path) -> StateManager:
    return StateManager(tmp_path / "state.db")


def test_reserve_vsock_cid_starts_at_pool_start(state: StateManager, tmp_path: Path) -> None:
    state.create_vm(_make_config(tmp_path, "vm-a"))
    cid = state.reserve_vsock_cid("vm-a")
    assert cid == VSOCK_CID_START
    assert state.get_vsock_cid("vm-a") == VSOCK_CID_START


def test_reserve_is_idempotent(state: StateManager, tmp_path: Path) -> None:
    state.create_vm(_make_config(tmp_path, "vm-a"))
    first = state.reserve_vsock_cid("vm-a")
    second = state.reserve_vsock_cid("vm-a")
    assert first == second


def test_distinct_vms_get_distinct_cids(state: StateManager, tmp_path: Path) -> None:
    state.create_vm(_make_config(tmp_path, "vm-a"))
    state.create_vm(_make_config(tmp_path, "vm-b"))
    cid_a = state.reserve_vsock_cid("vm-a")
    cid_b = state.reserve_vsock_cid("vm-b")
    assert cid_a != cid_b


def test_release_frees_cid_for_reuse(state: StateManager, tmp_path: Path) -> None:
    state.create_vm(_make_config(tmp_path, "vm-a"))
    cid = state.reserve_vsock_cid("vm-a")
    state.release_vsock_cid("vm-a")
    assert state.get_vsock_cid("vm-a") is None

    state.create_vm(_make_config(tmp_path, "vm-b"))
    assert state.reserve_vsock_cid("vm-b") == cid


def test_explicit_cid_request_for_restore(state: StateManager, tmp_path: Path) -> None:
    state.create_vm(_make_config(tmp_path, "vm-a"))
    cid = state.reserve_vsock_cid("vm-a", guest_cid=42)
    assert cid == 42
    assert state.get_vsock_cid("vm-a") == 42


def test_delete_vm_cascades_cid(state: StateManager, tmp_path: Path) -> None:
    state.create_vm(_make_config(tmp_path, "vm-a"))
    state.reserve_vsock_cid("vm-a")
    state.delete_vm("vm-a")
    assert state.get_vsock_cid("vm-a") is None
