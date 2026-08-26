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

"""Tests for Firecracker installation and discovery paths."""

from pathlib import Path

import pytest

from smolvm.host.paths import (
    SMOLVM_FIRECRACKER_DIR_ENV,
    directory_is_on_path,
    find_firecracker,
    resolve_firecracker_dir,
)


def _executable(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "firecracker"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    return binary


def test_explicit_directory_beats_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SMOLVM_FIRECRACKER_DIR_ENV, str(tmp_path / "environment"))

    assert resolve_firecracker_dir(tmp_path / "explicit") == (tmp_path / "explicit").resolve()


def test_environment_beats_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configured = tmp_path / "configured"
    monkeypatch.setenv(SMOLVM_FIRECRACKER_DIR_ENV, str(configured))

    assert resolve_firecracker_dir() == configured.resolve()


def test_empty_environment_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SMOLVM_FIRECRACKER_DIR_ENV, "   ")

    with pytest.raises(ValueError, match="SMOLVM_FIRECRACKER_DIR cannot be empty"):
        resolve_firecracker_dir()


def test_relative_explicit_directory_becomes_absolute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert resolve_firecracker_dir(Path("runtime/bin")) == (tmp_path / "runtime/bin").resolve()


def test_configured_directory_is_authoritative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path_binary = _executable(tmp_path / "path")
    monkeypatch.setenv(SMOLVM_FIRECRACKER_DIR_ENV, str(tmp_path / "missing"))

    assert find_firecracker(path_lookup=lambda _name: path_binary) is None


def test_path_is_used_before_user_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path_binary = _executable(tmp_path / "path")
    monkeypatch.delenv(SMOLVM_FIRECRACKER_DIR_ENV, raising=False)

    assert find_firecracker(path_lookup=lambda _name: path_binary) == path_binary.resolve()


def test_environment_changes_are_observed_after_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _executable(tmp_path / "first")
    second = _executable(tmp_path / "second")

    monkeypatch.setenv(SMOLVM_FIRECRACKER_DIR_ENV, str(first.parent))
    assert find_firecracker(path_lookup=lambda _name: None) == first

    monkeypatch.setenv(SMOLVM_FIRECRACKER_DIR_ENV, str(second.parent))
    assert find_firecracker(path_lookup=lambda _name: None) == second


def test_non_executable_file_is_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = tmp_path / "configured"
    configured.mkdir()
    (configured / "firecracker").write_text("not executable")
    monkeypatch.setenv(SMOLVM_FIRECRACKER_DIR_ENV, str(configured))

    assert find_firecracker(path_lookup=lambda _name: None) is None


def test_directory_is_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configured = tmp_path / "path with spaces"
    configured.mkdir()
    monkeypatch.setenv("PATH", f"/usr/bin:{configured}")

    assert directory_is_on_path(configured) is True
    assert directory_is_on_path(tmp_path / "elsewhere") is False
