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

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from scripts.benchmarks import ubuntu_transport


class _FakeConfig(SimpleNamespace):
    rootfs_path: Path


class _FakeSmolVM:
    restored_calls: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []

    def __init__(
        self,
        *,
        config: Any | None = None,
        ssh_key_path: str | None = None,
        comm_channel: str | None = None,
        **_: Any,
    ) -> None:
        self._vm_id = getattr(config, "vm_id", "bench-fake")
        self._ssh_key_path = ssh_key_path
        self._comm_channel = comm_channel
        self._control_channel = SimpleNamespace(kind=comm_channel)
        self._info = SimpleNamespace(
            network=SimpleNamespace(ssh_host_port=2201),
            config=SimpleNamespace(vsock=SimpleNamespace(guest_cid=1024)),
        )

    @classmethod
    def from_snapshot(cls, snapshot_id: str, **kwargs: Any) -> _FakeSmolVM:
        cls.restored_calls.append({"snapshot_id": snapshot_id, **kwargs})
        return cls(
            config=_FakeConfig(vm_id="restored", rootfs_path=Path("/tmp/restored.ext4")),
            ssh_key_path=kwargs.get("ssh_key_path"),
            comm_channel=kwargs.get("comm_channel"),
        )

    def start(self) -> None:
        pass

    def wait_for_ready(self, *, timeout: float) -> None:
        assert timeout == 60.0

    def run(self, command: str, *, shell: str, timeout: float) -> SimpleNamespace:
        assert shell == "raw"
        assert timeout == 10.0
        if command == "cat /proc/uptime":
            return SimpleNamespace(exit_code=0, stdout="0.42 0.01\n")
        assert command == "true"
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    def snapshot(self, *, snapshot_id: str, snapshot_type: Any) -> SimpleNamespace:
        self.snapshots.append(
            {"snapshot_id": snapshot_id, "snapshot_type": snapshot_type.value}
        )
        return SimpleNamespace(snapshot_id=snapshot_id)

    def stop(self, *, timeout: float = 15.0) -> None:
        pass

    def delete(self) -> None:
        pass


def test_snapshot_benchmark_restores_with_selected_transport(monkeypatch) -> None:
    _FakeSmolVM.restored_calls = []
    _FakeSmolVM.snapshots = []
    deleted_snapshots: list[str] = []
    config = _FakeConfig(vm_id="bench-fake", rootfs_path=Path("/tmp/rootfs.ext4"))

    monkeypatch.setattr(ubuntu_transport, "SmolVM", _FakeSmolVM)
    monkeypatch.setattr(
        ubuntu_transport,
        "_config_for_variant",
        lambda *_args, **_kwargs: (config, "/tmp/id_ed25519", Path("/tmp/rootfs.ext4"), None),
    )
    monkeypatch.setattr(ubuntu_transport, "_safe_teardown", lambda _vm: None)
    monkeypatch.setattr(
        ubuntu_transport,
        "_safe_delete_snapshot",
        lambda snapshot_id: deleted_snapshots.append(snapshot_id),
    )

    record = ubuntu_transport._run_snapshot_one(
        "qemu",
        "vsock",
        0,
        rootfs_source="published",
        warm_exec_runs=2,
        snapshot_choice="auto",
    )

    assert record["snapshot_type"] == "diff"
    assert record["source_control_kind"] == "vsock"
    assert record["restore_control_kind"] == "vsock"
    assert record["snapshot_restore_to_first_command_ms"] >= record["snapshot_restore_ms"]
    assert len(record["snapshot_warm_exec_ms"]) == 2
    assert deleted_snapshots == [record["snapshot_id"]]
    assert _FakeSmolVM.snapshots == [
        {"snapshot_id": record["snapshot_id"], "snapshot_type": "diff"}
    ]
    assert _FakeSmolVM.restored_calls == [
        {
            "snapshot_id": record["snapshot_id"],
            "backend": "qemu",
            "resume_vm": True,
            "ssh_key_path": "/tmp/id_ed25519",
            "comm_channel": "vsock",
        }
    ]


def test_run_benchmark_keeps_snapshot_lane_opt_in(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def _fake_group(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {kwargs["logger_prefix"]: {"summary": {}}}

    monkeypatch.setattr(ubuntu_transport, "_images_release_tag", lambda: "images-test")
    monkeypatch.setattr(ubuntu_transport, "_run_variant_group", _fake_group)

    without_snapshot = ubuntu_transport.run_benchmark(
        iterations=1,
        warm_exec_runs=1,
        rootfs_source="published",
        variants=(("qemu", "vsock"),),
    )
    assert "snapshot_variants" not in without_snapshot
    assert without_snapshot["selected_variants"] == ["qemu-vsock"]
    assert [call["logger_prefix"] for call in calls] == ["fresh"]
    assert calls[0]["variants"] == (("qemu", "vsock"),)

    calls.clear()
    with_snapshot = ubuntu_transport.run_benchmark(
        iterations=1,
        warm_exec_runs=1,
        rootfs_source="published",
        variants=(("qemu", "vsock"),),
        include_snapshot=True,
        snapshot_type="disk",
    )
    assert "snapshot_variants" in with_snapshot
    assert [call["logger_prefix"] for call in calls] == ["fresh", "snapshot"]
    assert calls[1]["variants"] == (("qemu", "vsock"),)
    assert calls[1]["runner_extra"] == {"snapshot_choice": "disk"}


def test_parse_variants_accepts_all_and_deduplicates_selected_variants() -> None:
    assert ubuntu_transport._parse_variants("all") == ubuntu_transport.ALL_VARIANTS
    assert ubuntu_transport._parse_variants("qemu-vsock, qemu-vsock,firecracker-vsock") == (
        ("qemu", "vsock"),
        ("firecracker", "vsock"),
    )


def test_parse_variants_rejects_unknown_variant() -> None:
    try:
        ubuntu_transport._parse_variants("qemu-bad")
    except ValueError as exc:
        assert "qemu-bad" in str(exc)
        assert "qemu-vsock" in str(exc)
        assert (
            "uv run python scripts/benchmarks/ubuntu_transport.py --variants qemu-vsock"
            in str(exc)
        )
    else:
        raise AssertionError("expected invalid variant to fail")
