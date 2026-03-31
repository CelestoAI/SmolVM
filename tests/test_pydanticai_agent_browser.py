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

"""Unit tests for the PydanticAI agent-browser example helpers."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "examples" / "agent_tools" / "pydanticai_agent_browser.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("pydanticai_agent_browser", MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_parse_browser_start_output_extracts_session_metadata() -> None:
    module = _load_module()
    payload = {
        "command": "browser.start",
        "ok": True,
        "data": {
            "session_id": "browser-abc123",
            "cdp_url": "http://127.0.0.1:39222",
            "live_url": "http://127.0.0.1:36080/vnc.html?autoconnect=1&resize=scale",
            "artifacts_dir": "/tmp/browser-abc123",
        },
    }

    parsed = module._parse_browser_start_output(json.dumps(payload))

    assert parsed is not None
    assert parsed["session_id"] == "browser-abc123"
    assert parsed["cdp_url"] == "http://127.0.0.1:39222"
    assert parsed["cdp_port"] == 39222
    assert parsed["live_url"] == "http://127.0.0.1:36080/vnc.html?autoconnect=1&resize=scale"
    assert parsed["artifacts_dir"] == "/tmp/browser-abc123"


def test_parse_browser_start_output_returns_none_for_non_json() -> None:
    module = _load_module()

    parsed = module._parse_browser_start_output("not json")

    assert parsed is None


def test_parse_browser_start_output_requires_cdp_url() -> None:
    module = _load_module()
    payload = {
        "command": "browser.start",
        "ok": True,
        "data": {
            "session_id": "browser-abc123",
            "cdp_url": None,
        },
    }

    with pytest.raises(RuntimeError, match="usable cdp_url"):
        module._parse_browser_start_output(json.dumps(payload))


def test_format_command_result_includes_normalized_browser_section() -> None:
    module = _load_module()
    session = {
        "session_id": "browser-abc123",
        "cdp_url": "http://127.0.0.1:39222",
        "cdp_port": 39222,
        "live_url": "http://127.0.0.1:36080/vnc.html",
        "artifacts_dir": "/tmp/browser-abc123",
    }

    formatted = module._format_command_result(0, '{"ok": true}', "", parsed_browser_session=session)

    assert "exit_code: 0" in formatted
    assert "stdout:\n{\"ok\": true}" in formatted
    assert "stderr:\n<empty>" in formatted
    assert "parsed_browser_session:" in formatted
    assert "session_id: browser-abc123" in formatted
    assert "cdp_port: 39222" in formatted
    assert "artifacts_dir: /tmp/browser-abc123" in formatted


def test_run_host_bash_logs_command_and_result(monkeypatch, capsys) -> None:
    module = _load_module()

    def fake_run(*args, **kwargs):
        del args, kwargs
        return module.subprocess.CompletedProcess(
            args=["bash", "-lc", "echo hi"],
            returncode=0,
            stdout="hello\n",
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    ctx = SimpleNamespace(deps=module.BrowserCliDeps())

    result = module.run_host_bash(ctx, "echo hi")

    captured = capsys.readouterr()
    assert "Running command: echo hi" in captured.err
    assert "Command result:" in captured.err
    assert "stdout:\nhello" in captured.err
    assert result.startswith("exit_code: 0")


def test_prompt_tells_model_to_read_help_and_plan_first() -> None:
    module = _load_module()

    assert "First read `agent-browser --help`." in module.SYSTEM_INSTRUCTIONS
    assert "Decide on the exact commands before you run them." in module.SYSTEM_INSTRUCTIONS
    assert "Read `agent-browser --help`" in module.DEMO_PROMPT
    assert "Make a short plan" in module.DEMO_PROMPT
