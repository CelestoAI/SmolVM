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

from scripts.benchmarks import bench
from scripts.benchmarks.boot_telemetry import parse_boot_telemetry_text, summarize_boot_telemetry
from scripts.benchmarks.metrics import stats


def _sample_log() -> str:
    return """
[    0.012345] Linux version 6.12.0
SMOLVM_TS stage=init-start epoch_s=1781280000 uptime_s=0.10
SMOLVM_TS stage=mounts-ready epoch_s=1781280000 uptime_s=0.12
SMOLVM_TS stage=root-ready epoch_s=1781280000 uptime_s=0.15
SMOLVM_TS stage=guest-agent-start epoch_s=1781280000 uptime_s=0.16
SMOLVM_TS stage=guest-agent-started epoch_s=1781280000 uptime_s=0.18
SMOLVM_TS stage=net-config-start epoch_s=1781280000 uptime_s=0.20
SMOLVM_TS stage=net-ready epoch_s=1781280000 uptime_s=0.30
SMOLVM_TS stage=ssh-hostkey-check-start epoch_s=1781280000 uptime_s=0.31
SMOLVM_TS stage=ssh-hostkey-check-done epoch_s=1781280000 uptime_s=0.55
SMOLVM_TS stage=ssh-authkey-inject-start epoch_s=1781280000 uptime_s=0.56
SMOLVM_TS stage=ssh-authkey-inject-done epoch_s=1781280000 uptime_s=0.57
SMOLVM_TS stage=sshd-start epoch_s=1781280000 uptime_s=0.58
SMOLVM_TS stage=sshd-invoked epoch_s=1781280000 uptime_s=0.80
SMOLVM_TS stage=init-complete epoch_s=1781280000 uptime_s=0.82
[    0.901234] random: crng init done
"""


def test_parse_boot_telemetry_text_extracts_markers_offsets_and_phases() -> None:
    telemetry = parse_boot_telemetry_text(_sample_log())

    assert telemetry["available"] is True
    assert telemetry["kernel_last_printk_s"] == 0.901
    assert telemetry["guest_init_markers_s"]["init-start"] == 0.1
    assert telemetry["guest_init_offsets_ms"]["sshd-invoked"] == 700.0
    assert telemetry["guest_init_phases_ms"]["guest_agent_start_ms"] == 20.0
    assert telemetry["guest_init_phases_ms"]["network_config_ms"] == 100.0
    assert telemetry["guest_init_phases_ms"]["ssh_hostkey_check_ms"] == 240.0
    assert telemetry["guest_init_phases_ms"]["init_to_complete_ms"] == 720.0


def test_summarize_boot_telemetry_collects_numeric_sections() -> None:
    telemetry = parse_boot_telemetry_text(_sample_log())

    summary = summarize_boot_telemetry([{"boot_telemetry": telemetry}], stats)

    assert summary["kernel_last_printk_s"]["p50"] == 0.9
    assert summary["guest_init_offsets_ms"]["sshd-invoked"]["p50"] == 700.0
    assert summary["guest_init_phases_ms"]["ssh_hostkey_check_ms"]["p50"] == 240.0


def test_bench_boot_attaches_boot_telemetry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    log_path = tmp_path / "bench-vm.log"
    log_path.write_text(_sample_log())

    class FakeVm:
        _vm_id = "bench-vm"

        def start(self) -> None:
            pass

        def wait_for_ssh(self) -> None:
            pass

        def run(self, command: str, *, shell: str) -> SimpleNamespace:
            assert command == "cat /proc/uptime"
            assert shell == "raw"
            return SimpleNamespace(stdout="0.95 0.10\n")

    monkeypatch.setattr(bench, "_new_vm", lambda backend: FakeVm())
    monkeypatch.setattr(bench, "_safe_teardown", lambda vm: None)
    monkeypatch.setattr(bench, "_vm_log_path", lambda vm: log_path)

    result = bench._bench_boot("qemu", 1, "cold-start")

    raw = result["raw"][0]
    assert raw["guest_uptime_at_first_command_s"] == 0.95
    assert raw["boot_telemetry"]["guest_init_offsets_ms"]["sshd-invoked"] == 700.0
    assert (
        result["boot_telemetry_stats"]["guest_init_phases_ms"]["init_to_sshd_invoked_ms"]["p50"]
        == 700.0
    )
