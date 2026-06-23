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

import json
from pathlib import Path

from scripts.benchmarks.reporting import (
    CommandPlan,
    cli_data,
    parse_json_output,
    run_command,
    start_report,
    write_json_report,
)


def test_run_command_dry_run_records_command_without_executing() -> None:
    record = run_command(
        CommandPlan("probe", ["smolvm", "doctor"], timeout_s=1.5),
        dry_run=True,
    )

    assert record["status"] == "dry-run"
    assert record["ok"] is True
    assert record["exit_code"] is None
    assert record["command"] == ["smolvm", "doctor"]
    assert record["command_text"] == "smolvm doctor"


def test_parse_json_output_tolerates_surrounding_text() -> None:
    payload = parse_json_output('notice\n{"data": {"vm": {"name": "bench"}}}\n')

    assert cli_data(payload) == {"vm": {"name": "bench"}}


def test_write_json_report_creates_parent_directory(tmp_path: Path) -> None:
    report, _started = start_report("probe", config={"x": 1}, dry_run=True)
    output = tmp_path / "nested" / "report.json"

    write_json_report(output, report)

    assert json.loads(output.read_text())["script"] == "probe"


def test_start_report_includes_shared_envelope_fields() -> None:
    config = {"iterations": 1, "backend": "qemu"}

    report, _started = start_report("probe", config=config, dry_run=True)

    assert report["schema_version"] == 1
    assert report["benchmark"] == "probe"
    assert report["parameters"] == config
    assert report["thresholds"] == {}
    assert report["variants"] == {}
    assert set(report) >= {
        "created_at",
        "git",
        "host",
        "smolvm_version",
        "smolvm_core",
    }
    assert {"commit", "branch", "dirty"} <= set(report["git"])
    assert {"system", "release", "machine", "python"} <= set(report["host"])
    assert isinstance(report["smolvm_version"], str)
    assert isinstance(report["smolvm_core"], str)
