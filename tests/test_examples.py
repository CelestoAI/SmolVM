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
import sys
import warnings
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "examples" / "agent_tools"
EXPECTED_EXAMPLES = {
    "computer_use_browser.py",
    "langchain_tool.py",
    "openai_agents_tool.py",
    "pydanticai_tool.py",
    "pydanticai_reusable_tool.py",
}


def test_agent_tool_examples_exist() -> None:
    """Ensure the planned agent-tool examples are present."""
    example_names = {path.name for path in EXAMPLES_DIR.glob("*.py")}
    assert example_names >= EXPECTED_EXAMPLES


def test_agent_tool_examples_compile() -> None:
    """Compile agent-tool examples without importing optional dependencies."""
    for path in sorted(EXAMPLES_DIR.glob("*.py")):
        py_compile.compile(str(path), doraise=True)


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_langchain_tool_import_without_pydantic_v1_warning() -> None:
    """Import the LangChain shell example without triggering Python 3.14 warnings."""
    path = EXAMPLES_DIR / "langchain_tool.py"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _load_module(path)

    messages = [str(warning.message) for warning in caught]
    assert not any("Pydantic V1 functionality" in message for message in messages)
