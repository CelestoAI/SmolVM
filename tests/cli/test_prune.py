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

"""Tests for smolvm prune (image-cache pruning)."""

from __future__ import annotations

from pathlib import Path

from smolvm.cli.prune import find_stale_caches


class TestFindStaleCaches:
    def test_returns_empty_when_no_cache_dir(self, tmp_path: Path) -> None:
        assert find_stale_caches(cache_dir=tmp_path / "nonexistent") == []

    def test_returns_empty_when_only_current_version(self, tmp_path: Path) -> None:
        (tmp_path / "openclaw-v1.0.0-amd64-firecracker").mkdir()
        (tmp_path / "base-kernel-v1.0.0-arm64").mkdir()
        assert find_stale_caches(cache_dir=tmp_path, current_version="1.0.0") == []

    def test_identifies_old_version_dirs(self, tmp_path: Path) -> None:
        current = tmp_path / "openclaw-v1.0.0-amd64-firecracker"
        stale1 = tmp_path / "openclaw-v0.0.13-amd64-firecracker"
        stale2 = tmp_path / "base-kernel-v0.0.14a0-arm64"
        current.mkdir()
        stale1.mkdir()
        stale2.mkdir()
        result = find_stale_caches(cache_dir=tmp_path, current_version="1.0.0")
        assert set(result) == {stale1, stale2}

    def test_ignores_unversioned_dirs(self, tmp_path: Path) -> None:
        (tmp_path / "s3").mkdir()
        (tmp_path / "some-random-dir").mkdir()
        (tmp_path / "openclaw-v0.0.13-amd64-firecracker").mkdir()
        result = find_stale_caches(cache_dir=tmp_path, current_version="1.0.0")
        assert len(result) == 1
        assert result[0].name == "openclaw-v0.0.13-amd64-firecracker"

    def test_ignores_files(self, tmp_path: Path) -> None:
        (tmp_path / "openclaw-v0.0.13-amd64-firecracker").write_text("not a dir")
        assert find_stale_caches(cache_dir=tmp_path, current_version="1.0.0") == []

    def test_alpha_version_treated_as_distinct(self, tmp_path: Path) -> None:
        (tmp_path / "openclaw-v0.0.14a0-amd64-qemu").mkdir()
        (tmp_path / "openclaw-v0.0.14-amd64-qemu").mkdir()
        result = find_stale_caches(cache_dir=tmp_path, current_version="0.0.14")
        assert len(result) == 1
        assert "0.0.14a0" in result[0].name
