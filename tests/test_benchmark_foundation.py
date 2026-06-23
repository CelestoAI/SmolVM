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

from scripts.benchmarks import artifacts, browser_ready, preset_start, runtime_control


def test_artifacts_dry_run_json_reports_scan_plan(capsys, tmp_path: Path) -> None:
    path = tmp_path / "missing"

    rc = artifacts.main(["--dry-run", "--json", "--path", str(path)])

    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["script"] == "artifacts"
    assert report["dry_run"] is True
    assert report["records"] == [
        {
            "hash_files": False,
            "max_entries": 5000,
            "max_hash_bytes": 536870912,
            "path": str(path),
            "status": "dry-run",
            "would_scan": True,
        }
    ]


def test_preset_start_dry_run_json_plans_preset_start_and_cleanup(capsys) -> None:
    rc = preset_start.main(
        [
            "--dry-run",
            "--json",
            "--preset",
            "codex",
            "--iterations",
            "1",
            "--name-prefix",
            "bench",
        ]
    )

    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    record = report["records"][0]
    assert record["start"]["command"][:3] == ["smolvm", "codex", "start"]
    assert "--no-attach" in record["start"]["command"]
    assert record["cleanup"]["command"][:3] == ["smolvm", "sandbox", "delete"]


def test_browser_ready_dry_run_json_plans_browser_start_and_stop(capsys) -> None:
    rc = browser_ready.main(
        [
            "--dry-run",
            "--json",
            "--session-id",
            "browser-bench",
            "--no-cdp-poll",
        ]
    )

    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    record = report["records"][0]
    assert record["start"]["command"][:3] == ["smolvm", "browser", "start"]
    assert record["start"]["command"][3:5] == ["--session-id", "browser-bench"]
    assert record["cleanup"]["command"] == ["smolvm", "browser", "stop", "browser-bench"]


def test_runtime_control_dry_run_json_plans_noun_verb_lifecycle(capsys) -> None:
    rc = runtime_control.main(
        [
            "--dry-run",
            "--json",
            "--operations",
            "info,stop,start",
            "--name-prefix",
            "bench-runtime",
        ]
    )

    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    record = report["records"][0]
    assert record["create"]["command"][:3] == ["smolvm", "sandbox", "create"]
    assert [step["command"][1:3] for step in record["operations"]] == [
        ["sandbox", "info"],
        ["sandbox", "stop"],
        ["sandbox", "start"],
    ]
    assert record["cleanup"]["command"][:3] == ["smolvm", "sandbox", "delete"]
