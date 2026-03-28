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

"""Unit tests for the OpenAI computer-use browser example helpers."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "examples" / "agent_tools" / "computer_use_browser.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("computer_use_browser", MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_build_config_uses_env_overrides(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setenv("COMPUTER_USE_MODEL", "gpt-5.4-mini")
    monkeypatch.setenv("SMOLVM_BROWSER_MODE", "headless")

    args = argparse.Namespace(
        task="Summarize the homepage.",
        start_url="https://example.com",
        allowed_domain=["docs.example.com"],
        mode=None,
        max_steps=5,
    )

    config = module._build_config(args)

    assert config.browser_mode == "headless"
    assert config.model == "gpt-5.4-mini"
    assert config.allowed_domains == ("example.com", "docs.example.com")


def test_is_allowed_url_only_accepts_allowlisted_hosts() -> None:
    module = _load_module()
    allowed = ("celesto.ai", "www.celesto.ai")

    assert module._is_allowed_url("https://celesto.ai/blog", allowed) is True
    assert module._is_allowed_url("about:blank", allowed) is True
    assert module._is_allowed_url("https://blog.celesto.ai", allowed) is False
    assert module._is_allowed_url("https://example.com", allowed) is False


def test_normalize_key_maps_common_names() -> None:
    module = _load_module()

    assert module._normalize_key("CTRL") == "Control"
    assert module._normalize_key("SPACE") == " "
    assert module._normalize_key("a") == "a"


def test_describe_action_formats_click_and_type() -> None:
    module = _load_module()

    class ClickAction:
        type = "click"
        button = "left"
        x = 120
        y = 45
        keys = ["CTRL"]

    class TypeAction:
        type = "type"
        text = "hello world"

    assert module._describe_action(ClickAction()) == "Control+click left @ (120, 45)"
    assert module._describe_action(TypeAction()) == "type 'hello world'"


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
