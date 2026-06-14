#!/usr/bin/env python3
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

"""Parse guest boot timing markers from SmolVM runtime logs."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

_SMOLVM_TS_RE = re.compile(
    r"\bSMOLVM_TS\s+stage=(?P<stage>\S+)"
    r"\s+epoch_s=(?P<epoch_s>\S+)"
    r"\s+uptime_s=(?P<uptime_s>\S+)"
)
_PRINTK_RE = re.compile(r"\[\s*(?P<stamp>\d+\.\d+)\]")

_PHASE_PAIRS: dict[str, tuple[str, str]] = {
    "mount_filesystems_ms": ("init-start", "mounts-ready"),
    "root_remount_ms": ("mounts-ready", "root-ready"),
    "guest_agent_start_ms": ("guest-agent-start", "guest-agent-started"),
    "network_config_ms": ("net-config-start", "net-ready"),
    "clock_sync_setup_ms": ("clock-sync-start", "clock-sync-started"),
    "ssh_hostkey_check_ms": ("ssh-hostkey-check-start", "ssh-hostkey-check-done"),
    "ssh_authkey_inject_ms": ("ssh-authkey-inject-start", "ssh-authkey-inject-done"),
    "sshd_start_ms": ("sshd-start", "sshd-invoked"),
    "init_to_guest_agent_started_ms": ("init-start", "guest-agent-started"),
    "init_to_network_ready_ms": ("init-start", "net-ready"),
    "init_to_sshd_invoked_ms": ("init-start", "sshd-invoked"),
    "init_to_complete_ms": ("init-start", "init-complete"),
}

_ALTERNATE_PHASE_ENDS: dict[str, tuple[str, ...]] = {
    "clock_sync_setup_ms": ("clock-sync-started", "clock-sync-disabled"),
}


def parse_boot_telemetry_text(text: str) -> dict[str, Any]:
    """Return guest boot telemetry parsed from one runtime log."""
    markers: dict[str, float] = {}
    marker_epochs: dict[str, float] = {}
    marker_order: list[str] = []

    for match in _SMOLVM_TS_RE.finditer(text):
        stage = match.group("stage")
        try:
            uptime_s = float(match.group("uptime_s"))
        except ValueError:
            continue
        try:
            epoch_s = float(match.group("epoch_s"))
        except ValueError:
            epoch_s = 0.0

        if stage not in markers:
            marker_order.append(stage)
        markers[stage] = round(uptime_s, 3)
        marker_epochs[stage] = round(epoch_s, 3)

    offsets_ms: dict[str, float] = {}
    init_start = markers.get("init-start")
    if init_start is not None:
        offsets_ms = {
            stage: round((markers[stage] - init_start) * 1000, 1) for stage in marker_order
        }

    phases_ms: dict[str, float] = {}
    for phase_name, (start_stage, default_end_stage) in _PHASE_PAIRS.items():
        end_stage = _resolve_phase_end(markers, phase_name, default_end_stage)
        if start_stage in markers and end_stage in markers:
            phases_ms[phase_name] = round((markers[end_stage] - markers[start_stage]) * 1000, 1)

    printk_stamps = [float(match.group("stamp")) for match in _PRINTK_RE.finditer(text)]
    kernel_last_printk_s = round(printk_stamps[-1], 3) if printk_stamps else None

    return {
        "available": bool(markers or kernel_last_printk_s is not None),
        "log_lines": text.count("\n"),
        "kernel_last_printk_s": kernel_last_printk_s,
        "guest_init_markers_s": {stage: markers[stage] for stage in marker_order},
        "guest_init_marker_epochs_s": {stage: marker_epochs[stage] for stage in marker_order},
        "guest_init_offsets_ms": offsets_ms,
        "guest_init_phases_ms": phases_ms,
    }


def collect_boot_telemetry(log_path: Path | None) -> dict[str, Any]:
    """Read and parse boot telemetry from a runtime log path."""
    if log_path is None:
        return {"available": False, "reason": "vm log path unavailable"}
    if not log_path.exists():
        return {"available": False, "reason": "vm log missing"}

    return parse_boot_telemetry_text(log_path.read_text(errors="replace"))


def summarize_boot_telemetry(
    records: list[dict[str, Any]],
    stat_fn: Callable[[list[float]], dict[str, Any]],
) -> dict[str, Any]:
    """Summarize parsed boot telemetry attached to raw benchmark records."""
    offsets: dict[str, list[float]] = {}
    phases: dict[str, list[float]] = {}
    kernel_last_printk_s: list[float] = []

    for record in records:
        telemetry = record.get("boot_telemetry")
        if not isinstance(telemetry, dict):
            continue
        _collect_numeric_section(telemetry.get("guest_init_offsets_ms"), offsets)
        _collect_numeric_section(telemetry.get("guest_init_phases_ms"), phases)
        kernel_stamp = telemetry.get("kernel_last_printk_s")
        if isinstance(kernel_stamp, int | float) and not isinstance(kernel_stamp, bool):
            kernel_last_printk_s.append(float(kernel_stamp))

    summary: dict[str, Any] = {}
    if offsets:
        summary["guest_init_offsets_ms"] = {
            name: stat_fn(values) for name, values in sorted(offsets.items())
        }
    if phases:
        summary["guest_init_phases_ms"] = {
            name: stat_fn(values) for name, values in sorted(phases.items())
        }
    if kernel_last_printk_s:
        summary["kernel_last_printk_s"] = stat_fn(kernel_last_printk_s)
    return summary


def _resolve_phase_end(
    markers: dict[str, float],
    phase_name: str,
    default_end_stage: str,
) -> str:
    for stage in _ALTERNATE_PHASE_ENDS.get(phase_name, ()):
        if stage in markers:
            return stage
    return default_end_stage


def _collect_numeric_section(section: object, bucket: dict[str, list[float]]) -> None:
    if not isinstance(section, dict):
        return
    for name, value in section.items():
        if isinstance(value, int | float) and not isinstance(value, bool):
            bucket.setdefault(str(name), []).append(float(value))
