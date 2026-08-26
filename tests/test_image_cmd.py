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

"""Tests for smolvm image list/rm and the SMOLVM_IMAGE_DIR resolution."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm import __version__
from smolvm.cli.image import _IMAGE_DIR_NAME_RE, _KERNEL_DIR_NAME_RE
from smolvm.cli.main import main
from smolvm.cli.prune import _format_bytes, _total_size, find_stale_caches
from smolvm.images.manager import IMAGE_DIR_ENV, ImageManager, resolve_image_dir


def _make_cache_dirs(root: Path) -> None:
    """Populate an image dir with representative cache entries."""
    entries = {
        "codex-v0.0.1-amd64-firecracker": 5,
        "claude-code-v0.0.1-arm64-qemu-alpine": 3,
        f"codex-v{__version__}-arm64-qemu": 7,
        "base-kernel-v0.0.1-amd64": 2,
    }
    for name, size in entries.items():
        d = root / name
        d.mkdir(parents=True)
        (d / "blob").write_bytes(b"x" * size)
    (root / "s3").mkdir()
    (root / "_guest-agent").mkdir()
    (root / "stray-file.txt").write_text("not a dir")


class TestResolveImageDir:
    def test_default_is_home_smolvm_images(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(IMAGE_DIR_ENV, raising=False)
        assert resolve_image_dir() == Path.home() / ".smolvm" / "images"

    def test_env_var_overrides_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(IMAGE_DIR_ENV, str(tmp_path / "custom"))
        assert resolve_image_dir() == tmp_path / "custom"

    def test_explicit_arg_beats_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(IMAGE_DIR_ENV, str(tmp_path / "from-env"))
        assert resolve_image_dir(tmp_path / "explicit") == tmp_path / "explicit"
        assert resolve_image_dir(str(tmp_path / "explicit")) == tmp_path / "explicit"

    def test_blank_env_var_falls_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(IMAGE_DIR_ENV, "  ")
        assert resolve_image_dir() == Path.home() / ".smolvm" / "images"

    def test_blank_explicit_arg_falls_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--image-dir "$UNSET"` must never target the current directory."""
        monkeypatch.delenv(IMAGE_DIR_ENV, raising=False)
        assert resolve_image_dir("") == Path.home() / ".smolvm" / "images"
        assert resolve_image_dir("   ") == Path.home() / ".smolvm" / "images"
        monkeypatch.setenv(IMAGE_DIR_ENV, str(tmp_path / "env"))
        assert resolve_image_dir("") == tmp_path / "env"

    def test_unknown_user_tilde_does_not_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """expanduser failures fall back to the path as written (regression:
        a '~typo/...' value used to escape as a raw RuntimeError)."""
        monkeypatch.delenv(IMAGE_DIR_ENV, raising=False)
        assert resolve_image_dir("~nosuchuser-xyz/dir") == Path("~nosuchuser-xyz/dir")
        monkeypatch.setenv(IMAGE_DIR_ENV, "~nosuchuser-xyz/dir")
        assert resolve_image_dir() == Path("~nosuchuser-xyz/dir")

    def test_does_not_create_the_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "never-created"
        monkeypatch.setenv(IMAGE_DIR_ENV, str(target))
        resolve_image_dir()
        assert not target.exists()

    def test_image_manager_honors_env_set_after_import(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolution happens at construction time, not module-import time."""
        monkeypatch.setenv(IMAGE_DIR_ENV, str(tmp_path / "late-env"))
        assert ImageManager().cache_dir == tmp_path / "late-env"

    def test_image_manager_explicit_cache_dir_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(IMAGE_DIR_ENV, str(tmp_path / "from-env"))
        assert ImageManager(cache_dir=tmp_path / "explicit").cache_dir == tmp_path / "explicit"

    def test_guest_agent_cache_dir_honors_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from smolvm.images.builder import _guest_agent_binary_cache_dir

        monkeypatch.setenv(IMAGE_DIR_ENV, str(tmp_path / "img"))
        assert _guest_agent_binary_cache_dir() == tmp_path / "img" / "_guest-agent"

    def test_image_builder_honors_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from smolvm.images.builder import ImageBuilder

        monkeypatch.setenv(IMAGE_DIR_ENV, str(tmp_path / "img"))
        assert ImageBuilder().cache_dir == tmp_path / "img"

    def test_find_stale_caches_honors_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stale = tmp_path / "codex-v0.0.1-amd64-firecracker"
        stale.mkdir()
        monkeypatch.setenv(IMAGE_DIR_ENV, str(tmp_path))
        assert find_stale_caches(current_version="9.9.9") == [stale]


class TestSizeHelpers:
    def test_format_bytes_keeps_fractions(self) -> None:
        assert _format_bytes(0) == "0.0 B"
        assert _format_bytes(1536) == "1.5 KiB"
        assert _format_bytes(2_040_109_465) == "1.9 GiB"

    def test_total_size_is_sparse_aware(self, tmp_path: Path) -> None:
        sparse = tmp_path / "rootfs.ext4"
        with sparse.open("wb") as f:
            f.truncate(64 * 1024 * 1024)  # 64 MiB apparent, ~0 allocated
            f.write(b"x")
        apparent = sparse.stat().st_size
        on_disk = _total_size(tmp_path)
        assert apparent >= 64 * 1024 * 1024
        if getattr(os.stat(sparse), "st_blocks", None) is not None:
            assert on_disk < apparent

    def test_total_size_tolerates_missing_path(self, tmp_path: Path) -> None:
        assert _total_size(tmp_path / "gone") == 0


class TestCacheNameParsing:
    def test_round_trips_every_manifest_entry(self) -> None:
        """The list/rm parser must recognize everything cache_name() emits."""
        from smolvm.images.published import MANIFEST, cache_name

        for preset, arch, vmm, os_name in MANIFEST:
            for version in ("0.0.26", "0.0.14a0", "0.0.24.post3", "1.2.3.dev1"):
                name = cache_name(preset, arch, vmm, version, os=os_name)
                match = _IMAGE_DIR_NAME_RE.match(name)
                assert match is not None, f"unparsed cache name: {name}"
                assert match["preset"] == preset
                assert match["version"] == version
                assert match["arch"] == arch
                assert match["vmm"] == vmm
                assert (match["os"] or "ubuntu") == os_name

    def test_kernel_names_round_trip(self) -> None:
        for version in ("0.0.26", "0.0.24.post3"):
            name = f"base-kernel-v{version}-amd64"
            match = _KERNEL_DIR_NAME_RE.match(name)
            assert match is not None
            assert match["version"] == version

    def test_prune_matches_dotted_suffix_versions(self, tmp_path: Path) -> None:
        """Shipped 0.0.24.postN caches must be prunable (regression)."""
        stale = tmp_path / "codex-v0.0.24.post3-amd64-firecracker"
        stale.mkdir()
        assert find_stale_caches(cache_dir=tmp_path, current_version="9.9.9") == [stale]


class TestImageList:
    def test_list_json_classifies_entries(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _make_cache_dirs(tmp_path)

        ret = main(["image", "list", "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "image.list"
        assert payload["ok"] is True
        assert payload["data"]["image_dir"] == str(tmp_path)
        rows = {row["name"]: row for row in payload["data"]["images"]}
        # The stray file is skipped; the six directories are classified.
        assert len(rows) == 6

        codex = rows["codex-v0.0.1-amd64-firecracker"]
        assert codex["kind"] == "image"
        assert codex["preset"] == "codex"
        assert codex["arch"] == "amd64"
        assert codex["vmm"] == "firecracker"
        assert codex["os"] == "ubuntu"
        assert codex["current"] is False
        assert codex["size_bytes"] == _total_size(tmp_path / "codex-v0.0.1-amd64-firecracker")
        assert codex["size_bytes"] > 0

        # Non-greedy preset parse: hyphenated preset with alpine suffix.
        claude = rows["claude-code-v0.0.1-arm64-qemu-alpine"]
        assert claude["preset"] == "claude-code"
        assert claude["os"] == "alpine"
        assert claude["vmm"] == "qemu"

        current = rows[f"codex-v{__version__}-arm64-qemu"]
        assert current["current"] is True

        kernel = rows["base-kernel-v0.0.1-amd64"]
        assert kernel["kind"] == "kernel"
        assert kernel["arch"] == "amd64"
        assert kernel["preset"] is None

        assert rows["s3"]["kind"] == "other"
        assert rows["s3"]["current"] is None
        assert rows["_guest-agent"]["kind"] == "other"

        assert payload["data"]["total_size_bytes"] == sum(
            row["size_bytes"] for row in payload["data"]["images"]
        )

    def test_list_classifies_dotted_suffix_versions(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Shipped 0.0.24.postN caches parse as images, not 'other' (regression)."""
        (tmp_path / "codex-v0.0.24.post3-amd64-firecracker").mkdir(parents=True)

        ret = main(["image", "list", "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        row = payload["data"]["images"][0]
        assert row["kind"] == "image"
        assert row["preset"] == "codex"
        assert row["version"] == "0.0.24.post3"
        assert row["current"] is False

    def test_list_missing_dir_is_empty(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        ret = main(["image", "list", "--image-dir", str(tmp_path / "missing"), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["images"] == []
        assert payload["data"]["total_size_bytes"] == 0

    def test_list_human_empty_names_pull(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ret = main(["image", "list", "--image-dir", str(tmp_path)])

        assert ret == 0
        # Rich wraps the panel mid-word with box-drawing borders; strip
        # everything but letters/digits so the recovery command is findable.
        flattened = re.sub(r"[^a-z0-9]+", "", capsys.readouterr().out.lower())
        assert "smolvmimagepullcodex" in flattened

    def test_list_env_var_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _make_cache_dirs(tmp_path)
        monkeypatch.setenv(IMAGE_DIR_ENV, str(tmp_path))

        ret = main(["image", "list", "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["image_dir"] == str(tmp_path)
        assert len(payload["data"]["images"]) == 6

    def test_list_unreadable_dir_reports_error_envelope(
        self, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Permission failures produce the JSON envelope, not a traceback."""

        def boom(self: Path) -> None:
            raise PermissionError(13, "Permission denied", str(self))

        monkeypatch.setattr(Path, "iterdir", boom)

        ret = main(["image", "list", "--image-dir", str(tmp_path), "--json"])

        assert ret == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert "permission" in payload["error"]["recovery"].lower()


class TestImageRm:
    def test_rm_exact_name(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        _make_cache_dirs(tmp_path)
        expected_freed = _total_size(tmp_path / "codex-v0.0.1-amd64-firecracker")

        ret = main(
            [
                "image",
                "rm",
                "codex-v0.0.1-amd64-firecracker",
                "--image-dir",
                str(tmp_path),
                "--json",
            ]
        )

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "image.rm"
        assert [e["name"] for e in payload["data"]["removed"]] == ["codex-v0.0.1-amd64-firecracker"]
        assert payload["data"]["freed_bytes"] == expected_freed
        assert expected_freed > 0
        assert not (tmp_path / "codex-v0.0.1-amd64-firecracker").exists()
        # The other codex version is untouched by an exact-name removal.
        assert (tmp_path / f"codex-v{__version__}-arm64-qemu").exists()

    def test_rm_accepts_trailing_slash_and_list_path(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Tab-completed names and the path `image list` prints both work."""
        _make_cache_dirs(tmp_path)

        ret = main(
            [
                "image",
                "rm",
                "codex-v0.0.1-amd64-firecracker/",
                "--image-dir",
                str(tmp_path),
                "--json",
            ]
        )
        assert ret == 0
        capsys.readouterr()
        assert not (tmp_path / "codex-v0.0.1-amd64-firecracker").exists()

        full_path = str(tmp_path / "base-kernel-v0.0.1-amd64")
        ret = main(["image", "rm", full_path, "--image-dir", str(tmp_path), "--json"])
        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert [e["name"] for e in payload["data"]["removed"]] == ["base-kernel-v0.0.1-amd64"]

    def test_rm_by_preset_removes_all_versions(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _make_cache_dirs(tmp_path)
        (tmp_path / "codex-v0.0.24.post3-amd64-firecracker").mkdir()

        ret = main(["image", "rm", "codex", "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        removed = {e["name"] for e in payload["data"]["removed"]}
        assert removed == {
            "codex-v0.0.1-amd64-firecracker",
            f"codex-v{__version__}-arm64-qemu",
            "codex-v0.0.24.post3-amd64-firecracker",
        }
        assert payload["data"]["freed_bytes"] > 0
        # Unrelated entries survive preset-wide removal.
        assert (tmp_path / "s3").exists()
        assert (tmp_path / "_guest-agent").exists()
        assert (tmp_path / "base-kernel-v0.0.1-amd64").exists()
        assert (tmp_path / "claude-code-v0.0.1-arm64-qemu-alpine").exists()

    @pytest.mark.parametrize(
        ("alias", "expected"),
        [
            ("claude", "claude-code-v0.0.1-arm64-qemu-alpine"),
            ("claw", "openclaw-v0.0.1-amd64-firecracker"),
        ],
    )
    def test_rm_preset_aliases(
        self, alias: str, expected: str, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Aliases come from the presets registry, not a hardcoded map."""
        _make_cache_dirs(tmp_path)
        (tmp_path / "openclaw-v0.0.1-amd64-firecracker").mkdir()

        ret = main(["image", "rm", alias, "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert [e["name"] for e in payload["data"]["removed"]] == [expected]

    def test_rm_dry_run_removes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _make_cache_dirs(tmp_path)

        ret = main(["image", "rm", "codex", "--dry-run", "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["dry_run"] is True
        assert payload["data"]["would_free_bytes"] > 0
        assert (tmp_path / "codex-v0.0.1-amd64-firecracker").exists()

    def test_rm_unknown_name_fails_with_recovery(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _make_cache_dirs(tmp_path)

        ret = main(["image", "rm", "nope", "--image-dir", str(tmp_path), "--json"])

        assert ret == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert "smolvm image list" in payload["error"]["recovery"]

    @pytest.mark.parametrize("name", ["../evil", "a/b", "..", ".", "/somewhere/else/evil"])
    def test_rm_rejects_traversal_names(
        self, name: str, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        (tmp_path / "evil").mkdir()

        ret = main(["image", "rm", name, "--image-dir", str(tmp_path / "sub"), "--json"])

        assert ret == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"]["code"] == "invalid_input"
        assert (tmp_path / "evil").exists()

    def test_rm_symlink_unlinks_without_touching_target(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Symlink entries are unlinked in place — never followed, never
        rmtree'd (regression: rmtree on a symlink used to crash)."""
        root = tmp_path / "images"
        root.mkdir()
        real = root / "codex-v0.0.1-amd64-firecracker"
        real.mkdir()
        (real / "rootfs.ext4").write_text("data")
        (root / "old-codex").symlink_to(real)

        ret = main(["image", "rm", "old-codex", "--image-dir", str(root), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert [e["name"] for e in payload["data"]["removed"]] == ["old-codex"]
        assert payload["data"]["freed_bytes"] == 0
        assert not (root / "old-codex").exists()
        assert (real / "rootfs.ext4").exists()

    def test_rm_symlink_outside_root_only_removes_link(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        root = tmp_path / "images"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "data").write_text("keep me")
        (root / "escape").symlink_to(outside)

        ret = main(["image", "rm", "escape", "--image-dir", str(root), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert [e["name"] for e in payload["data"]["removed"]] == ["escape"]
        assert not (root / "escape").exists()
        assert (outside / "data").exists()


class TestPruneAlias:
    def test_top_level_prune_keeps_command_name(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ret = main(["prune", "--cache-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "prune"

    def test_top_level_prune_accepts_image_dir(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """The documented alias takes the same --image-dir flag as the group."""
        stale = tmp_path / "codex-v0.0.1-amd64-firecracker"
        stale.mkdir()

        ret = main(["prune", "--image-dir", str(tmp_path), "--dry-run", "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "prune"
        assert payload["data"]["would_remove"]

    def test_image_prune_uses_dotted_command_name(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ret = main(["image", "prune", "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "image.prune"

    def test_image_prune_removes_stale_dirs(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _make_cache_dirs(tmp_path)

        ret = main(["image", "prune", "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        # Stale-versioned dirs go; current-version and unversioned stay.
        assert not (tmp_path / "codex-v0.0.1-amd64-firecracker").exists()
        assert not (tmp_path / "base-kernel-v0.0.1-amd64").exists()
        assert (tmp_path / f"codex-v{__version__}-arm64-qemu").exists()
        assert (tmp_path / "s3").exists()


class TestChoiceListsMatchPublishedTypes:
    def test_pull_option_choices_track_published_literals(self) -> None:
        """The hardcoded click.Choice lists must not drift from the manifest
        type literals (they are hardcoded to keep published.py off the CLI
        import path)."""
        import typing

        import click

        from smolvm.cli.main import build_cli
        from smolvm.images.published import Arch, Os, Vmm

        pull = build_cli().commands["image"].commands["pull"]  # type: ignore[attr-defined]
        choices = {
            param.name: set(param.type.choices)
            for param in pull.params
            if isinstance(param.type, click.Choice)
        }
        assert choices["arch"] == set(typing.get_args(Arch))
        assert choices["vmm"] == set(typing.get_args(Vmm))
        assert choices["os_name"] == set(typing.get_args(Os))


class TestRelativeTime:
    def test_buckets(self) -> None:
        from datetime import datetime

        from smolvm.cli.image import _relative_time

        now = datetime(2026, 7, 17, 12, 0, 0, tzinfo=UTC)

        def at(seconds_ago: int) -> str:
            from datetime import timedelta

            return (now - timedelta(seconds=seconds_ago)).isoformat()

        assert _relative_time(at(0), now=now) == "0 seconds ago"
        assert _relative_time(at(1), now=now) == "1 second ago"
        assert _relative_time(at(90), now=now) == "1 minute ago"
        assert _relative_time(at(7200), now=now) == "2 hours ago"
        assert _relative_time(at(86400 * 3), now=now) == "3 days ago"
        assert _relative_time(at(86400 * 8), now=now) == "1 week ago"
        assert _relative_time(at(86400 * 40), now=now) == "1 month ago"
        assert _relative_time(at(86400 * 400), now=now) == "1 year ago"
        assert _relative_time(None, now=now) == "-"
        assert _relative_time("not-a-date", now=now) == "-"

    def test_list_rows_carry_parseable_created(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        from datetime import datetime

        _make_cache_dirs(tmp_path)
        ret = main(["image", "list", "--image-dir", str(tmp_path), "--json"])
        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        for row in payload["data"]["images"]:
            assert row["created"] is not None
            datetime.fromisoformat(row["created"])


class TestAliases:
    def test_top_level_images_envelope(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        _make_cache_dirs(tmp_path)
        ret = main(["images", "--image-dir", str(tmp_path), "--json"])
        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "images"
        assert len(payload["data"]["images"]) == 6

    def test_image_ls_keeps_list_envelope(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _make_cache_dirs(tmp_path)
        ret = main(["image", "ls", "--image-dir", str(tmp_path), "--json"])
        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "image.list"


# 32 MiB. `sparse_copy` decides per chunk by content, so the source's own hole
# map is irrelevant; what decides whether the TARGET ends up sparse is its size
# on the volume the test runs on. On one macOS volume a written extent below
# ~16 MiB comes back fully allocated while 32 MiB does not, and other APFS
# volumes on the same machine punch holes from 1 MiB. The mechanism is not
# identified, so treat this as a margin rather than a bound.
_SPARSE_HOLE_BYTES = 32 * 1024 * 1024


def _make_published_entry(root: Path, name: str) -> Path:
    """A realistic published cache dir: kernel, zst, sparse rootfs, sidecar."""
    import hashlib

    import zstandard

    d = root / name
    d.mkdir(parents=True)
    raw = b"\x00" * _SPARSE_HOLE_BYTES + b"REAL-DATA"
    (d / "rootfs.ext4.zst").write_bytes(zstandard.compress(raw))
    sha = hashlib.sha256((d / "rootfs.ext4.zst").read_bytes()).hexdigest()
    (d / "rootfs.ext4.from-sha256").write_text(f"sparse-v1:{sha}")
    (d / "vmlinux.bin").write_bytes(b"kernel-bytes")
    with open(d / "rootfs.ext4", "wb") as f:
        f.truncate(len(raw))
        f.seek(_SPARSE_HOLE_BYTES)
        f.write(b"REAL-DATA")
    return d


class TestImageInspect:
    def test_inspect_exact_name_returns_array(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _make_published_entry(tmp_path, "codex-v0.0.1-amd64-firecracker")

        ret = main(
            [
                "image",
                "inspect",
                "codex-v0.0.1-amd64-firecracker",
                "--image-dir",
                str(tmp_path),
                "--json",
            ]
        )

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "image.inspect"
        assert isinstance(payload["data"], list) and len(payload["data"]) == 1
        entry = payload["data"][0]
        assert entry["kind"] == "image"
        assert entry["rootfs_sidecar"].startswith("sparse-v1:")
        files = {f["name"]: f for f in entry["files"]}
        assert set(files) == {
            "rootfs.ext4",
            "rootfs.ext4.from-sha256",
            "rootfs.ext4.zst",
            "vmlinux.bin",
        }
        rootfs = files["rootfs.ext4"]
        if os.name == "posix":
            assert rootfs["size_on_disk_bytes"] < rootfs["size_bytes"]
        # Stale version: no manifest section.
        assert entry["manifest"] is None

    def test_inspect_current_version_gets_manifest(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        from smolvm.images.published import IMAGES_RELEASE_TAG, cache_name

        _make_published_entry(tmp_path, cache_name("codex", "amd64", "firecracker"))

        ret = main(["image", "inspect", "codex", "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        entry = payload["data"][0]
        assert entry["current"] is True
        manifest = entry["manifest"]
        assert manifest is not None
        assert manifest["images_release_tag"] == IMAGES_RELEASE_TAG
        assert len(manifest["rootfs_sha256"]) == 64
        assert manifest["kernel_url"].startswith("https://")

    def test_inspect_preset_matches_all_variants(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _make_cache_dirs(tmp_path)

        ret = main(["image", "inspect", "codex", "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["data"]) == 2

    def test_inspect_unknown_name(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        ret = main(["image", "inspect", "nope", "--image-dir", str(tmp_path), "--json"])

        assert ret == 1
        payload = json.loads(capsys.readouterr().out)
        assert "smolvm image list" in payload["error"]["recovery"]


class TestImagePullAll:
    @pytest.mark.parametrize("argv", [["image", "pull"], ["image", "pull", "codex", "--all"]])
    def test_mutual_exclusion(self, argv: list[str], capsys: pytest.CaptureFixture) -> None:
        ret = main([*argv, "--json"])

        assert ret == 2
        payload = json.loads(capsys.readouterr().out)
        assert "Choose one target" in payload["error"]["message"]

    @patch("smolvm.images.published.ensure_published_image")
    @patch("smolvm.cli.main._vmm_for_host", return_value="firecracker")
    @patch("smolvm.cli.main._host_arch_for_published", return_value="amd64")
    def test_pull_all_covers_manifest(
        self,
        mock_arch: MagicMock,
        mock_vmm: MagicMock,
        mock_ensure: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        from smolvm.images.manager import LocalImage
        from smolvm.images.published import MANIFEST

        kernel = tmp_path / "vmlinux.bin"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()
        mock_ensure.return_value = LocalImage(name="x", kernel_path=kernel, rootfs_path=rootfs)

        ret = main(["image", "pull", "--all", "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "image.pull"
        expected = sorted(
            {(p, o) for (p, a, v, o) in MANIFEST if a == "amd64" and v == "firecracker"}
        )
        assert len(payload["data"]["pulled"]) == len(expected)
        assert payload["data"]["failed"] == []
        called = sorted((c.args[0], c.args[3]) for c in mock_ensure.call_args_list)
        assert called == expected

    @patch("smolvm.images.published.ensure_published_image")
    @patch("smolvm.cli.main._vmm_for_host", return_value="firecracker")
    @patch("smolvm.cli.main._host_arch_for_published", return_value="amd64")
    def test_pull_all_isolates_failures(
        self,
        mock_arch: MagicMock,
        mock_vmm: MagicMock,
        mock_ensure: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        from smolvm.exceptions import ImageError
        from smolvm.images.manager import LocalImage

        kernel = tmp_path / "vmlinux.bin"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        def flaky(preset: str, *args: object, **kwargs: object) -> LocalImage:
            if preset == "codex":
                raise ImageError("boom")
            return LocalImage(name="x", kernel_path=kernel, rootfs_path=rootfs)

        mock_ensure.side_effect = flaky

        ret = main(["image", "pull", "--all", "--image-dir", str(tmp_path), "--json"])

        assert ret == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        details = payload["error"]["details"]
        assert {f["preset"] for f in details["failed"]} == {"codex"}
        assert details["pulled"]  # other presets still downloaded
        assert "smolvm image pull --all" in payload["error"]["recovery"]

    @patch("smolvm.images.published.ensure_published_image")
    @patch("smolvm.cli.main._vmm_for_host", return_value="firecracker")
    @patch("smolvm.cli.main._host_arch_for_published", return_value="amd64")
    def test_pull_all_os_filter(
        self,
        mock_arch: MagicMock,
        mock_vmm: MagicMock,
        mock_ensure: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        from smolvm.images.manager import LocalImage

        kernel = tmp_path / "vmlinux.bin"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()
        mock_ensure.return_value = LocalImage(name="x", kernel_path=kernel, rootfs_path=rootfs)

        ret = main(
            ["image", "pull", "--all", "--os", "alpine", "--image-dir", str(tmp_path), "--json"]
        )

        assert ret == 0
        capsys.readouterr()
        assert all(c.args[3] == "alpine" for c in mock_ensure.call_args_list)


class TestCustomImages:
    def _make_custom(self, root: Path, name: str = "myimg") -> Path:
        fp = root / "custom" / name / "abc123def4567890"
        fp.mkdir(parents=True)
        (fp / "metadata.json").write_text(json.dumps({"name": name, "arch": "amd64"}))
        (fp / "rootfs.ext4").write_bytes(b"rootfs")
        return fp

    def test_list_shows_custom_rows(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        self._make_custom(tmp_path)

        ret = main(["image", "list", "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        rows = json.loads(capsys.readouterr().out)["data"]["images"]
        assert len(rows) == 1
        row = rows[0]
        assert row["kind"] == "custom"
        assert row["name"] == "custom/myimg"
        assert row["version"] == "abc123def456"
        assert row["arch"] == "amd64"

    def test_rm_custom_name_removes_tree(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        self._make_custom(tmp_path)

        ret = main(["image", "rm", "custom/myimg", "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert [e["name"] for e in payload["data"]["removed"]] == ["custom/myimg"]
        assert not (tmp_path / "custom" / "myimg").exists()

    def test_rm_custom_fingerprint(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        fp = self._make_custom(tmp_path)

        ret = main(
            ["image", "rm", f"custom/myimg/{fp.name}", "--image-dir", str(tmp_path), "--json"]
        )

        assert ret == 0
        capsys.readouterr()
        assert not fp.exists()
        assert (tmp_path / "custom" / "myimg").exists()

    @pytest.mark.parametrize("bad", ["custom/../x", "custom//x", "custom/a/b/c", "other/x"])
    def test_rm_rejects_bad_custom_names(
        self, bad: str, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ret = main(["image", "rm", bad, "--image-dir", str(tmp_path), "--json"])

        assert ret == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"]["code"] == "invalid_input"


class TestRmSafety:
    def test_rm_bare_custom_is_rejected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """'custom' is the namespace, not an entry — one rm must never wipe
        every build (regression)."""
        fp = tmp_path / "custom" / "myimg" / "abc123def4567890"
        fp.mkdir(parents=True)
        (fp / "rootfs.ext4").write_bytes(b"x")

        ret = main(["image", "rm", "custom", "--image-dir", str(tmp_path), "--json"])

        assert ret == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"]["code"] == "invalid_input"
        assert fp.exists()

    def test_rm_refuses_symlinked_middle_component(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A symlinked component under custom/ must not redirect the delete
        at a different entry (regression)."""
        published = tmp_path / "codex-v9.9.9-amd64-firecracker"
        published.mkdir()
        (published / "rootfs.ext4").write_bytes(b"keep me")
        (tmp_path / "custom").mkdir()
        (tmp_path / "custom" / "link").symlink_to(tmp_path)

        ret = main(
            [
                "image",
                "rm",
                "custom/link/codex-v9.9.9-amd64-firecracker",
                "--image-dir",
                str(tmp_path),
                "--json",
            ]
        )

        assert ret == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert (published / "rootfs.ext4").exists()

    def test_rm_accepts_unusual_directory_names(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Any real top-level directory is removable, not just charset-clean
        names (regression: 'my old image' became undeletable)."""
        odd = tmp_path / "my old image"
        odd.mkdir()
        (odd / "blob").write_bytes(b"x")

        ret = main(["image", "rm", "my old image", "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        capsys.readouterr()
        assert not odd.exists()

    def test_rm_fingerprint_prefix(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Docker-style short ids: the 12-char version shown by list works."""
        a = tmp_path / "custom" / "web" / "aaaa1111bbbb2222"
        b = tmp_path / "custom" / "web" / "cccc3333dddd4444"
        a.mkdir(parents=True)
        b.mkdir(parents=True)

        ret = main(
            ["image", "rm", "custom/web/aaaa1111bbbb", "--image-dir", str(tmp_path), "--json"]
        )

        assert ret == 0
        capsys.readouterr()
        assert not a.exists()
        assert b.exists()

    def test_rm_ambiguous_fingerprint_prefix(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        a = tmp_path / "custom" / "web" / "aaaa1111bbbb2222"
        b = tmp_path / "custom" / "web" / "aaaa1111cccc3333"
        a.mkdir(parents=True)
        b.mkdir(parents=True)

        ret = main(["image", "rm", "custom/web/aaaa", "--image-dir", str(tmp_path), "--json"])

        assert ret == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"]["code"] == "invalid_input"
        assert "aaaa1111bbbb2222" in payload["error"]["message"]
        assert "aaaa1111cccc3333" in payload["error"]["message"]
        assert a.exists() and b.exists()

    def test_list_shows_top_level_dot_dirs(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Orphaned staging dirs must not be invisible disk usage."""
        staging = tmp_path / ".codex-v0.0.1-amd64-firecracker.partial"
        staging.mkdir()
        (staging / "rootfs.ext4").write_bytes(b"x" * 10)

        ret = main(["image", "list", "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        rows = json.loads(capsys.readouterr().out)["data"]["images"]
        assert [r["kind"] for r in rows] == ["other"]
        assert rows[0]["size_bytes"] > 0


class TestPullAllRecovery:
    @patch("smolvm.images.published.ensure_published_image")
    @patch("smolvm.cli.main._vmm_for_host", return_value="firecracker")
    @patch("smolvm.cli.main._host_arch_for_published", return_value="amd64")
    def test_failure_recovery_keeps_flags(
        self,
        mock_arch: MagicMock,
        mock_vmm: MagicMock,
        mock_ensure: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        from smolvm.exceptions import ImageError

        mock_ensure.side_effect = ImageError("boom")

        ret = main(["image", "pull", "--all", "--image-dir", str(tmp_path), "--json"])

        assert ret == 1
        payload = json.loads(capsys.readouterr().out)
        assert "--image-dir" in payload["error"]["recovery"]

    @patch("smolvm.images.published.published_targets", return_value=[])
    @patch("smolvm.cli.main._vmm_for_host", return_value="firecracker")
    @patch("smolvm.cli.main._host_arch_for_published", return_value="amd64")
    def test_no_targets_names_recovery(
        self,
        mock_arch: MagicMock,
        mock_vmm: MagicMock,
        mock_targets: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        ret = main(
            ["image", "pull", "--all", "--os", "alpine", "--image-dir", str(tmp_path), "--json"]
        )

        assert ret == 2
        payload = json.loads(capsys.readouterr().out)
        recovery = payload["error"]["recovery"]
        assert "smolvm image pull --all" in recovery
        assert "--os" not in recovery


class TestUpstreamReviewRegressions:
    def test_image_ls_parse_error_reports_list_envelope(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """Usage errors on the ls alias must report the canonical
        image.list command (regression: argv fallback said image.ls)."""
        ret = main(["image", "ls", "--not-a-flag", "--json"])

        assert ret == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "image.list"

    def test_prune_unlinks_symlinked_stale_entries(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Prune must unlink symlinked entries, not crash rmtree, and must
        never follow the link (regression)."""
        real = tmp_path / "real-data"
        real.mkdir()
        (real / "keep").write_text("keep")
        (tmp_path / "codex-v0.0.1-amd64-firecracker").symlink_to(real)

        ret = main(["prune", "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        capsys.readouterr()
        assert not (tmp_path / "codex-v0.0.1-amd64-firecracker").exists()
        assert (real / "keep").exists()

    def test_guest_agent_cache_dir_honors_explicit_cache_dir(self, tmp_path: Path) -> None:
        """An explicit builder cache_dir must reach the guest-agent cache
        (regression: it always used the global default)."""
        from smolvm.images.builder import _guest_agent_binary_cache_dir

        assert _guest_agent_binary_cache_dir(tmp_path) == tmp_path / "_guest-agent"

    @pytest.mark.parametrize("name", ["custom/.hidden", "custom/web/.fp"])
    def test_dot_prefixed_custom_segments_rejected(
        self, name: str, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Dot-prefixed custom names would be invisible to image list."""
        ret = main(["image", "rm", name, "--image-dir", str(tmp_path), "--json"])

        assert ret == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"]["code"] == "invalid_input"
