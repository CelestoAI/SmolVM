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

"""Tests for example scripts that rely on optional third-party packages."""

from __future__ import annotations

import importlib.util
import py_compile
import re
import sys
import warnings
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
README_PATH = REPO_ROOT / "README.md"
TOP_LEVEL_EXAMPLES_DIR = REPO_ROOT / "examples"
AGENT_TOOL_EXAMPLES_DIR = TOP_LEVEL_EXAMPLES_DIR / "agent_tools"
EXPECTED_TOP_LEVEL_EXAMPLES = {
    "browser_session.py",
    "env_injection.py",
    "openclaw.py",
    "quickstart_sandbox.py",
}
EXPECTED_AGENT_TOOL_EXAMPLES = {
    "computer_use_browser.py",
    "langchain_tool.py",
    "openai_agents_tool.py",
    "pydanticai_tool.py",
    "pydanticai_reusable_tool.py",
}
README_USE_CASE_LINKS = {
    "examples/quickstart_sandbox.py",
    "examples/browser_session.py",
    "examples/agent_tools/computer_use_browser.py",
    "examples/agent_tools/openai_agents_tool.py",
    "examples/agent_tools/langchain_tool.py",
    "examples/agent_tools/pydanticai_tool.py",
    "examples/agent_tools/pydanticai_reusable_tool.py",
    "examples/env_injection.py",
}


def test_agent_tool_examples_exist() -> None:
    """Ensure the planned agent-tool examples are present."""
    example_names = {path.name for path in AGENT_TOOL_EXAMPLES_DIR.glob("*.py")}
    assert example_names >= EXPECTED_AGENT_TOOL_EXAMPLES


def test_top_level_examples_exist() -> None:
    """Ensure the top-level examples linked from the README are present."""
    example_names = {path.name for path in TOP_LEVEL_EXAMPLES_DIR.glob("*.py")}
    assert example_names >= EXPECTED_TOP_LEVEL_EXAMPLES


def test_agent_tool_examples_compile() -> None:
    """Compile agent-tool examples without importing optional dependencies."""
    for path in sorted(AGENT_TOOL_EXAMPLES_DIR.glob("*.py")):
        py_compile.compile(str(path), doraise=True)


def test_top_level_examples_compile() -> None:
    """Compile top-level examples without running them."""
    for path in sorted(TOP_LEVEL_EXAMPLES_DIR.glob("*.py")):
        py_compile.compile(str(path), doraise=True)


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("example_name", sorted(EXPECTED_AGENT_TOOL_EXAMPLES))
def test_agent_tool_examples_import_without_optional_dependencies(example_name: str) -> None:
    """Import agent-tool examples without requiring optional dependencies."""
    _load_module(AGENT_TOOL_EXAMPLES_DIR / example_name)


@pytest.mark.parametrize("example_name", sorted(EXPECTED_TOP_LEVEL_EXAMPLES))
def test_top_level_examples_import_without_optional_dependencies(example_name: str) -> None:
    """Import top-level examples without requiring optional dependencies."""
    _load_module(TOP_LEVEL_EXAMPLES_DIR / example_name)


def test_langchain_tool_import_without_pydantic_v1_warning() -> None:
    """Import the LangChain shell example without triggering Python 3.14 warnings."""
    path = AGENT_TOOL_EXAMPLES_DIR / "langchain_tool.py"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _load_module(path)

    messages = [str(warning.message) for warning in caught]
    assert not any("Pydantic V1 functionality" in message for message in messages)


def _readme_section(title: str) -> str:
    content = README_PATH.read_text(encoding="utf-8")
    match = re.search(
        rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)",
        content,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"README section '{title}' not found"
    return match.group(1)


def test_readme_use_case_links_resolve_to_local_files() -> None:
    """README use-case links should point to real repo files."""
    section = _readme_section("Use cases")
    links = {
        link
        for link in re.findall(r"\[[^\]]+\]\(([^)]+)\)", section)
        if not link.startswith(("http://", "https://", "#", "mailto:"))
    }
    assert links >= README_USE_CASE_LINKS
    for link in links:
        assert (REPO_ROOT / link).exists(), f"README link target not found: {link}"
