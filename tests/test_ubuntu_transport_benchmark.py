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


def _sample_log() -> str:
    return """
SMOLVM_TS stage=init-start epoch_s=1781280000 uptime_s=0.10
SMOLVM_TS stage=guest-agent-start epoch_s=1781280000 uptime_s=0.20
SMOLVM_TS stage=guest-agent-started epoch_s=1781280000 uptime_s=0.24
SMOLVM_TS stage=net-config-start epoch_s=1781280000 uptime_s=0.30
SMOLVM_TS stage=net-ready epoch_s=1781280000 uptime_s=0.40
SMOLVM_TS stage=ssh-hostkey-check-start epoch_s=1781280000 uptime_s=0.42
SMOLVM_TS stage=ssh-hostkey-check-done epoch_s=1781280000 uptime_s=0.62
SMOLVM_TS stage=sshd-start epoch_s=1781280000 uptime_s=0.64
SMOLVM_TS stage=sshd-invoked epoch_s=1781280000 uptime_s=0.72
SMOLVM_TS stage=init-complete epoch_s=1781280000 uptime_s=0.74
"""


def test_fresh_benchmark_attaches_boot_telemetry(monkeypatch, tmp_path: Path) -> None:
    config = _FakeConfig(vm_id="bench-fake", rootfs_path=Path("/tmp/rootfs.ext4"))
    log_path = tmp_path / "bench-fake.log"
    log_path.write_text(_sample_log())

    monkeypatch.setattr(ubuntu_transport, "SmolVM", _FakeSmolVM)
    monkeypatch.setattr(
        ubuntu_transport,
        "_config_for_variant",
        lambda *_args, **_kwargs: (config, "/tmp/id_ed25519", Path("/tmp/rootfs.ext4"), None),
    )
    monkeypatch.setattr(ubuntu_transport, "_safe_teardown", lambda _vm: None)
    monkeypatch.setattr(ubuntu_transport, "_vm_log_path", lambda _vm: log_path)

    record = ubuntu_transport._run_one(
        "qemu",
        "ssh",
        0,
        rootfs_source="published",
        warm_exec_runs=1,
    )
    summary = ubuntu_transport._summarize([record])

    assert record["boot_telemetry"]["guest_init_phases_ms"]["ssh_hostkey_check_ms"] == 200.0
    assert (
        summary["boot_telemetry_stats"]["guest_init_phases_ms"]["ssh_hostkey_check_ms"][
            "median"
        ]
        == 200.0
    )


def test_snapshot_benchmark_restores_with_selected_transport(monkeypatch, tmp_path: Path) -> None:
    _FakeSmolVM.restored_calls = []
    _FakeSmolVM.snapshots = []
    deleted_snapshots: list[str] = []
    config = _FakeConfig(vm_id="bench-fake", rootfs_path=Path("/tmp/rootfs.ext4"))
    source_log = tmp_path / "bench-fake.log"
    restored_log = tmp_path / "restored.log"
    source_log.write_text(_sample_log())
    restored_log.write_text(_sample_log())
    logs = {"bench-fake": source_log, "restored": restored_log}

    monkeypatch.setattr(ubuntu_transport, "SmolVM", _FakeSmolVM)
    monkeypatch.setattr(
        ubuntu_transport,
        "_config_for_variant",
        lambda *_args, **_kwargs: (config, "/tmp/id_ed25519", Path("/tmp/rootfs.ext4"), None),
    )
    monkeypatch.setattr(ubuntu_transport, "_safe_teardown", lambda _vm: None)
    monkeypatch.setattr(
        ubuntu_transport,
        "_vm_log_path",
        lambda vm: logs[vm._vm_id],
    )
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
    assert (
        record["snapshot_source_boot_telemetry"]["guest_init_phases_ms"][
            "ssh_hostkey_check_ms"
        ]
        == 200.0
    )
    assert (
        record["snapshot_restore_boot_telemetry"]["guest_init_phases_ms"][
            "ssh_hostkey_check_ms"
        ]
        == 200.0
    )
    summary = ubuntu_transport._summarize_snapshot([record])
    assert (
        summary["snapshot_source_boot_telemetry_stats"]["guest_init_phases_ms"][
            "ssh_hostkey_check_ms"
        ]["median"]
        == 200.0
    )
    assert (
        summary["snapshot_restore_boot_telemetry_stats"]["guest_init_phases_ms"][
            "ssh_hostkey_check_ms"
        ]["median"]
        == 200.0
    )
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


def test_stats_include_tail_percentiles() -> None:
    stats = ubuntu_transport._stats([100.0, 200.0, 300.0])

    assert stats["median"] == 200.0
    assert stats["p90"] == 280.0
    assert stats["p95"] == 290.0


def test_format_variant_summary_table_includes_ready_and_guest_phases() -> None:
    report = {
        "variants": {
            "qemu-ssh": {
                "backend": "qemu",
                "transport": "ssh",
                "summary": {
                    "total_fresh_ready_ms": {"median": 1450.0, "p95": 1700.0},
                    "first_command_ms": {"median": 9.5},
                    "warm_exec_median_ms": {"median": 42.0},
                    "boot_telemetry_stats": {
                        "guest_init_phases_ms": {
                            "ssh_hostkey_check_ms": {"median": 290.0},
                            "network_config_ms": {"median": 10.0},
                        }
                    },
                },
            }
        }
    }

    table = ubuntu_transport._format_variant_summary_table(report)

    assert "| qemu | ssh | 1450.0 ms | 1700.0 ms | 9.5 ms | 42.0 ms |" in table
    assert "ssh_hostkey_check_ms=290.0 ms" in table


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
