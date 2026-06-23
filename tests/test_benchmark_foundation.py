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
from typing import Any

from scripts.benchmarks import (
    artifacts,
    browser_ready,
    disk_io,
    file_transfer,
    preset_start,
    runtime_control,
)


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


def test_disk_io_dry_run_json_reports_variants_and_operations(capsys) -> None:
    rc = disk_io.main(
        [
            "--dry-run",
            "--json",
            "--sizes",
            "1K",
            "--operations",
            "copy,decompress",
            "--variants",
            "native,forced-off",
        ]
    )

    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["script"] == "disk_io"
    assert report["dry_run"] is True
    assert [record["operation"] for record in report["records"]] == [
        "copy",
        "copy",
        "decompress",
        "decompress",
    ]
    assert {record["variant"] for record in report["records"]} == {"native", "forced-off"}
    assert {record["size_bytes"] for record in report["records"]} == {1024}


def test_file_transfer_dry_run_json_reports_files_and_directory(capsys) -> None:
    rc = file_transfer.main(
        [
            "--dry-run",
            "--json",
            "--sizes",
            "1K,2K",
            "--directory-files",
            "3",
            "--directory-file-size",
            "4K",
            "--backend",
            "qemu",
            "--comm-channel",
            "vsock",
        ]
    )

    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["script"] == "file_transfer"
    assert report["dry_run"] is True
    assert [record["operation"] for record in report["records"]] == [
        "file_round_trip",
        "file_round_trip",
        "directory_round_trip",
    ]
    assert report["records"][0]["backend"] == "qemu"
    assert report["records"][0]["comm_channel"] == "vsock"
    assert report["records"][2]["files"] == 3
    assert report["records"][2]["file_size_bytes"] == 4096


def test_preset_start_attempts_cleanup_after_start_failure(monkeypatch, capsys) -> None:
    calls: list[str] = []

    def fake_run_command(plan: Any, *, dry_run: bool = False) -> dict[str, Any]:
        calls.append(plan.label)
        return {
            "label": plan.label,
            "ok": False,
            "status": "failed",
            "duration_ms": 1.0,
            "dry_run": dry_run,
        }

    monkeypatch.setattr(preset_start, "run_command", fake_run_command)

    rc = preset_start.main(["--json", "--preset", "codex", "--iterations", "1"])

    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    record = report["records"][0]
    assert calls == ["preset_start", "sandbox_delete"]
    assert record["cleanup_expected"] is True
    assert record["cleanup"]["label"] == "sandbox_delete"


def test_browser_ready_attempts_cleanup_after_start_failure(monkeypatch, capsys) -> None:
    calls: list[str] = []

    def fake_run_command(plan: Any, *, dry_run: bool = False) -> dict[str, Any]:
        calls.append(plan.label)
        return {
            "label": plan.label,
            "ok": False,
            "status": "failed",
            "duration_ms": 1.0,
            "dry_run": dry_run,
        }

    monkeypatch.setattr(browser_ready, "run_command", fake_run_command)

    rc = browser_ready.main(["--json", "--session-id", "browser-bench", "--no-cdp-poll"])

    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    record = report["records"][0]
    assert calls == ["browser_start", "browser_stop"]
    assert record["cleanup_expected"] is True
    assert record["cleanup"]["label"] == "browser_stop"


def test_browser_ready_fails_when_cdp_poll_has_no_url(monkeypatch, capsys) -> None:
    def fake_run_command(plan: Any, *, dry_run: bool = False) -> dict[str, Any]:
        if plan.label == "browser_start":
            return {
                "label": plan.label,
                "ok": True,
                "status": "ok",
                "duration_ms": 1.0,
                "dry_run": dry_run,
                "stdout": json.dumps({"data": {"id": "browser-bench"}}),
            }
        return {
            "label": plan.label,
            "ok": True,
            "status": "ok",
            "duration_ms": 1.0,
            "dry_run": dry_run,
        }

    monkeypatch.setattr(browser_ready, "run_command", fake_run_command)

    rc = browser_ready.main(["--json", "--session-id", "browser-bench"])

    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    probe = report["records"][0]["cdp_probe"]
    assert probe["ready"] is False
    assert probe["attempts"] == 0


def test_runtime_control_attempts_cleanup_after_create_failure(monkeypatch, capsys) -> None:
    calls: list[str] = []

    def fake_run_command(plan: Any, *, dry_run: bool = False) -> dict[str, Any]:
        calls.append(plan.label)
        return {
            "label": plan.label,
            "ok": False,
            "status": "failed",
            "duration_ms": 1.0,
            "dry_run": dry_run,
        }

    monkeypatch.setattr(runtime_control, "run_command", fake_run_command)

    rc = runtime_control.main(["--json", "--iterations", "1"])

    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    record = report["records"][0]
    assert calls == ["sandbox_create", "sandbox_delete"]
    assert record["cleanup_expected"] is True
    assert record["cleanup"]["label"] == "sandbox_delete"


def test_expected_cleanup_failure_marks_records_failed() -> None:
    cleanup_failed = {"ok": False}

    assert not preset_start._record_ok(
        {"start": {"ok": True}, "cleanup_expected": True, "cleanup": cleanup_failed}
    )
    assert not browser_ready._record_ok(
        {"start": {"ok": True}, "cleanup_expected": True, "cleanup": cleanup_failed}
    )
    assert not runtime_control._record_ok(
        {
            "create": {"ok": True},
            "operations": [{"ok": True}],
            "cleanup_expected": True,
            "cleanup": cleanup_failed,
        }
    )
