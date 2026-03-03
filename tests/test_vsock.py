# Copyright 2026 Celesto AI

from pathlib import Path
from unittest.mock import MagicMock

from smolvm.api import FirecrackerClient
from smolvm.types import VMConfig, VMInfo, VMState
from smolvm.vm import derive_vsock_guest_cid, resolve_vsock_config


def test_firecracker_client_add_vsock_calls_expected_endpoint(tmp_path: Path) -> None:
    client = FirecrackerClient(tmp_path / "fc.sock")
    client._request = MagicMock(return_value=None)  # type: ignore[method-assign]

    client.add_vsock("control", guest_cid=42, uds_path=tmp_path / "vsock.sock")

    client._request.assert_called_once_with(  # type: ignore[attr-defined]
        "PUT",
        "/vsock/control",
        json={"vsock_id": "control", "guest_cid": 42, "uds_path": str(tmp_path / "vsock.sock")},
    )


def test_derive_vsock_guest_cid_is_stable_and_non_reserved() -> None:
    cid1 = derive_vsock_guest_cid("vm-alpha")
    cid2 = derive_vsock_guest_cid("vm-alpha")
    cid3 = derive_vsock_guest_cid("vm-beta")

    assert cid1 == cid2
    assert cid1 >= 3
    assert cid3 >= 3
    assert cid1 != cid3


def test_resolve_vsock_config_applies_defaults(tmp_path: Path) -> None:
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()

    config = VMConfig(vm_id="vmtest", kernel_path=kernel, rootfs_path=rootfs)
    info = VMInfo(vm_id="vmtest", status=VMState.CREATED, config=config)

    resolved = resolve_vsock_config(info, tmp_path)

    assert resolved.enabled is True
    assert resolved.guest_cid is not None
    assert resolved.guest_port == 5000
    assert resolved.uds_path == tmp_path / "vsock-vmtest.sock"
