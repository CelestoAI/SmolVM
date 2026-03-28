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

"""Unit tests for the LangChain browser example helpers."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "examples" / "agent_tools" / "langchain_browser_tool.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("langchain_browser_tool", MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_build_config_uses_env_overrides(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setenv("LANGCHAIN_MODEL", "openai:gpt-5.4-mini")
    monkeypatch.setenv("COMPUTER_USE_MODEL", "gpt-5.4-mini")
    monkeypatch.setenv("SMOLVM_BROWSER_MODE", "headless")

    config = module._build_config("https://example.com")

    assert config.browser_mode == "headless"
    assert config.langchain_model == "openai:gpt-5.4-mini"
    assert config.computer_use_model == "gpt-5.4-mini"
    assert "example.com" in config.allowed_domains


def test_is_allowed_url_only_accepts_allowlisted_hosts() -> None:
    module = _load_module()
    allowed = ("celesto.ai", "www.celesto.ai")

    assert module._is_allowed_url("https://celesto.ai/blog", allowed) is True
    assert module._is_allowed_url("about:blank", allowed) is True
    assert module._is_allowed_url("https://blog.celesto.ai", allowed) is False
    assert module._is_allowed_url("https://example.com", allowed) is False


def test_find_blocked_url_literals_reports_external_urls() -> None:
    module = _load_module()

    blocked = module._find_blocked_url_literals(
        'await page.goto("https://example.com"); await page.goto("https://celesto.ai/blog")',
        ("celesto.ai", "www.celesto.ai"),
    )

    assert blocked == ["https://example.com"]


def test_format_result_omits_missing_optional_fields() -> None:
    module = _load_module()
    result = module.ComputerUseResult(
        final_answer="Orchestrating Dinner with OpenClaw",
        session_id="browser-123",
        page_url="https://celesto.ai/blog",
        cdp_url=None,
        live_url="http://127.0.0.1:3999",
        artifacts_dir=None,
    )

    formatted = module._format_result(result)

    assert "answer: Orchestrating Dinner with OpenClaw" in formatted
    assert "session_id: browser-123" in formatted
    assert "live_url: http://127.0.0.1:3999" in formatted
    assert "cdp_url:" not in formatted
    assert "artifacts_dir:" not in formatted
