from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from smolvm.browser_profiles import (
    BrowserProfileCompatibilityError,
    BrowserProfileCorruptError,
    BrowserProfileError,
    BrowserProfileLockedError,
    BrowserProfileOutcomeUnknownError,
    BrowserProfileStore,
    _fsync_directory,
)


def store(root: Path, *, schema: int = 1, image: str = "linux-desktop-v1") -> BrowserProfileStore:
    return BrowserProfileStore(root, schema_version=schema, image_version=image)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def archive(tmp_path: Path, name: str, content: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_saves_versioned_digest_checked_generation_with_private_permissions(tmp_path: Path) -> None:
    root = tmp_path / "profiles"
    with store(root).acquire("work-profile") as lease:
        saved = lease.save(archive(tmp_path, "input.tar", b"profile archive"))
        loaded = lease.load()

    assert loaded is not None
    assert loaded.archive_path.read_bytes() == b"profile archive"
    assert loaded.manifest == saved.manifest
    assert loaded.manifest.manifest_version == 1
    assert loaded.manifest.schema_version == 1
    assert loaded.manifest.image_version == "linux-desktop-v1"
    assert len(loaded.manifest.archive_sha256) == 64
    assert mode(root) == 0o700
    assert mode(root / "work-profile") == 0o700
    assert mode(saved.path) == 0o700
    assert mode(saved.path / "profile.tar") == 0o600
    assert mode(saved.path / "manifest.json") == 0o600
    assert mode(root / "work-profile" / "CURRENT") == 0o600
    assert mode(root / "work-profile" / ".lock") == 0o600
    assert all(
        path.stat().st_uid == os.getuid()
        for path in (
            root,
            root / "work-profile",
            saved.path,
            saved.path / "profile.tar",
            saved.path / "manifest.json",
            root / "work-profile" / "CURRENT",
        )
    )


def test_save_and_load_stream_without_path_read_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = archive(tmp_path, "streamed.tar", b"x" * (2 * 1024 * 1024 + 17))

    def reject_read_bytes(_path: Path) -> bytes:
        raise AssertionError("profile archive must be streamed")

    monkeypatch.setattr(Path, "read_bytes", reject_read_bytes)
    with store(tmp_path / "profiles").acquire("streamed") as lease:
        saved = lease.save(source)
        loaded = lease.load()
    assert loaded is not None
    assert loaded.manifest.archive_sha256 == saved.manifest.archive_sha256


def test_profile_lease_is_exclusive_nonblocking_and_actionable(tmp_path: Path) -> None:
    profile_store = store(tmp_path / "profiles")
    with (
        profile_store.acquire("shared"),
        pytest.raises(BrowserProfileLockedError, match="Close its active browser session"),
    ):
        profile_store.acquire("shared")
    with profile_store.acquire("shared"):
        pass


def test_rejects_hostile_lock_symlink_without_touching_target(tmp_path: Path) -> None:
    root = tmp_path / "profiles"
    profile_dir = root / "hostile-lock"
    profile_dir.mkdir(parents=True)
    target = tmp_path / "outside-lock"
    target.write_text("outside", encoding="utf-8")
    (profile_dir / ".lock").symlink_to(target)

    with pytest.raises(BrowserProfileError, match="lock cannot be a symbolic link"):
        store(root).acquire("hostile-lock")

    assert target.read_text(encoding="utf-8") == "outside"
    assert mode(target) != 0o600


def test_unsupported_lock_host_fails_closed_with_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "fcntl", None)
    with pytest.raises(BrowserProfileError, match="Use a POSIX host"):
        store(tmp_path / "profiles").acquire("unsupported")


def test_failed_current_swap_preserves_prior_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_store = store(tmp_path / "profiles")
    with profile_store.acquire("crash-safe") as lease:
        first = lease.save(archive(tmp_path, "first.tar", b"first"))
        real_replace = os.replace

        def fail_pointer_swap(source: Path, destination: Path) -> None:
            if Path(destination).name == "CURRENT":
                raise OSError("simulated crash before pointer swap")
            real_replace(source, destination)

        monkeypatch.setattr(os, "replace", fail_pointer_swap)
        with pytest.raises(OSError, match="simulated crash"):
            lease.save(archive(tmp_path, "second.tar", b"second"))
        loaded = lease.load()

    assert loaded is not None
    assert loaded.archive_path.read_bytes() == b"first"
    assert loaded.manifest.generation == first.manifest.generation


def test_staged_directory_fsync_failure_preserves_prior_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "profiles"
    with store(root).acquire("staged-fsync") as lease:
        first = lease.save(archive(tmp_path, "fsync-first.tar", b"first"))
        real_fsync_directory = _fsync_directory

        def fail_staged_directory(path: Path) -> None:
            if path.name.startswith(".staging-"):
                raise OSError("simulated staged directory fsync failure")
            real_fsync_directory(path)

        monkeypatch.setattr("smolvm.browser_profiles._fsync_directory", fail_staged_directory)
        with pytest.raises(OSError, match="staged directory fsync failure"):
            lease.save(archive(tmp_path, "fsync-second.tar", b"second"))
        loaded = lease.load()

    assert loaded is not None
    assert loaded.manifest.generation == first.manifest.generation
    assert {path.name for path in (root / "staged-fsync" / "generations").iterdir()} == {
        first.manifest.generation
    }


def test_generation_directory_fsync_failure_never_switches_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "profiles"
    with store(root).acquire("generation-fsync") as lease:
        first = lease.save(archive(tmp_path, "generation-first.tar", b"first"))
        real_fsync_directory = _fsync_directory

        def fail_generation_directory(path: Path) -> None:
            if path.name == "generations":
                raise OSError("simulated generation directory fsync failure")
            real_fsync_directory(path)

        monkeypatch.setattr("smolvm.browser_profiles._fsync_directory", fail_generation_directory)
        with pytest.raises(OSError, match="generation directory fsync failure"):
            lease.save(archive(tmp_path, "generation-second.tar", b"second"))
        loaded = lease.load()

    assert loaded is not None
    assert loaded.manifest.generation == first.manifest.generation


def test_profile_directory_fsync_failure_reports_unknown_committed_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "profiles"
    with store(root).acquire("pointer-fsync") as lease:
        first = lease.save(archive(tmp_path, "pointer-first.tar", b"first"))
        real_fsync_directory = _fsync_directory

        def fail_profile_directory(path: Path) -> None:
            if path == root / "pointer-fsync":
                raise OSError("simulated profile directory fsync failure")
            real_fsync_directory(path)

        monkeypatch.setattr("smolvm.browser_profiles._fsync_directory", fail_profile_directory)
        with pytest.raises(BrowserProfileOutcomeUnknownError) as raised:
            lease.save(archive(tmp_path, "pointer-second.tar", b"second"))
        loaded = lease.load()

    assert loaded is not None
    assert loaded.manifest.generation == raised.value.generation_id
    assert loaded.manifest.generation != first.manifest.generation
    assert "Load the profile before trying again" in str(raised.value)


def test_save_retains_only_current_and_previous_generations(tmp_path: Path) -> None:
    root = tmp_path / "profiles"
    with store(root).acquire("bounded") as lease:
        first = lease.save(archive(tmp_path, "bounded-1.tar", b"one"))
        second = lease.save(archive(tmp_path, "bounded-2.tar", b"two"))
        third = lease.save(archive(tmp_path, "bounded-3.tar", b"three"))

        generation_ids = {path.name for path in (root / "bounded" / "generations").iterdir()}

    assert generation_ids == {
        second.manifest.generation,
        third.manifest.generation,
    }
    assert not first.path.exists()


def test_next_save_removes_orphan_from_failed_current_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "profiles"
    with store(root).acquire("orphan-cleanup") as lease:
        first = lease.save(archive(tmp_path, "orphan-1.tar", b"one"))
        real_replace = os.replace

        def fail_pointer_swap(source: Path, destination: Path) -> None:
            if Path(destination).name == "CURRENT":
                raise OSError("simulated pointer failure")
            real_replace(source, destination)

        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", fail_pointer_swap)
            with pytest.raises(OSError, match="simulated pointer failure"):
                lease.save(archive(tmp_path, "orphan-2.tar", b"two"))

        assert len(list((root / "orphan-cleanup" / "generations").iterdir())) == 2
        third = lease.save(archive(tmp_path, "orphan-3.tar", b"three"))
        generation_ids = {path.name for path in (root / "orphan-cleanup" / "generations").iterdir()}

    assert generation_ids == {first.manifest.generation, third.manifest.generation}


def test_incompatible_versions_require_explicit_reset_or_migration(tmp_path: Path) -> None:
    root = tmp_path / "profiles"
    with store(root).acquire("versioned") as lease:
        lease.save(archive(tmp_path, "v1.tar", b"version one"))

    with store(root, schema=2, image="linux-desktop-v2").acquire("versioned") as lease:
        with pytest.raises(BrowserProfileCompatibilityError, match=r"reset\(\) or migrate\(\)"):
            lease.load()

        def migrate(source: Path, destination: Path) -> None:
            destination.write_bytes(source.read_bytes() + b" migrated")

        migrated = lease.migrate(migrate)
        assert migrated.manifest.schema_version == 2
        assert migrated.manifest.image_version == "linux-desktop-v2"
        assert lease.load() == migrated
        assert migrated.archive_path.read_bytes() == b"version one migrated"


def test_reset_requires_an_open_lease_and_removes_all_generations(tmp_path: Path) -> None:
    profile_store = store(tmp_path / "profiles")
    lease = profile_store.acquire("resettable")
    saved = lease.save(archive(tmp_path, "temporary.tar", b"temporary"))
    lease.reset()
    assert lease.load() is None
    assert not saved.path.exists()
    lease.close()
    with pytest.raises(BrowserProfileError, match="lease is closed"):
        lease.reset()


@pytest.mark.parametrize(
    "profile_id",
    ["", ".", "..", "../escape", "nested/name", "/absolute", "white space", "x" * 65],
)
def test_rejects_unsafe_profile_ids(tmp_path: Path, profile_id: str) -> None:
    with pytest.raises(ValueError, match="profile_id"):
        store(tmp_path / "profiles").acquire(profile_id)
    assert not (tmp_path / "escape").exists()


def test_rejects_tampered_archive_and_invalid_current_pointer(tmp_path: Path) -> None:
    root = tmp_path / "profiles"
    with store(root).acquire("tampered") as lease:
        saved = lease.save(archive(tmp_path, "trusted.tar", b"trusted"))
        (saved.path / "profile.tar").write_bytes(b"changed")
        with pytest.raises(BrowserProfileCorruptError, match="integrity check"):
            lease.load()

    with store(root).acquire("bad-pointer") as lease:
        pointer = root / "bad-pointer" / "CURRENT"
        pointer.write_text("../../outside\n", encoding="utf-8")
        with pytest.raises(BrowserProfileCorruptError, match="invalid CURRENT"):
            lease.load()


def test_rejects_hostile_current_symlink_even_when_target_is_missing(tmp_path: Path) -> None:
    root = tmp_path / "profiles"
    with store(root).acquire("hostile-current") as lease:
        pointer = root / "hostile-current" / "CURRENT"
        pointer.symlink_to(tmp_path / "missing-outside-pointer")
        with pytest.raises(BrowserProfileCorruptError, match="invalid CURRENT"):
            lease.load()


def test_rejects_malformed_manifest_with_recovery_guidance(tmp_path: Path) -> None:
    root = tmp_path / "profiles"
    with store(root).acquire("bad-manifest") as lease:
        saved = lease.save(archive(tmp_path, "manifest-input.tar", b"trusted"))
        manifest_path = saved.path / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["archive_sha256"] = 7
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(BrowserProfileCorruptError, match=r"Call reset\(\)"):
            lease.load()


def test_failed_migration_callback_keeps_current_generation(tmp_path: Path) -> None:
    root = tmp_path / "profiles"
    with store(root).acquire("migration-crash") as lease:
        original = lease.save(archive(tmp_path, "original.tar", b"original"))

    with store(root, schema=2).acquire("migration-crash") as lease:

        def fail(_source: Path, _destination: Path) -> None:
            raise RuntimeError("migration failed")

        with pytest.raises(RuntimeError, match="migration failed"):
            lease.migrate(fail)
        current_pointer = (root / "migration-crash" / "CURRENT").read_text().strip()
        assert current_pointer == original.manifest.generation

    with store(root).acquire("migration-crash") as lease:
        loaded = lease.load()
        assert loaded is not None
        assert loaded.archive_path.read_bytes() == b"original"


def test_migration_callback_cannot_mutate_canonical_generation(tmp_path: Path) -> None:
    root = tmp_path / "profiles"
    with store(root).acquire("migration-copy") as lease:
        original = lease.save(archive(tmp_path, "copy-original.tar", b"original"))

    with store(root, schema=2).acquire("migration-copy") as lease:

        def corrupt_source_then_fail(source: Path, _destination: Path) -> None:
            source.chmod(0o600)
            source.write_bytes(b"corrupted private copy")
            raise RuntimeError("migration failed")

        with pytest.raises(RuntimeError, match="migration failed"):
            lease.migrate(corrupt_source_then_fail)

    with store(root).acquire("migration-copy") as lease:
        loaded = lease.load()
        assert loaded is not None
        assert loaded.manifest.generation == original.manifest.generation
        assert loaded.archive_path.read_bytes() == b"original"


def test_manifest_does_not_describe_a_whole_vm_disk(tmp_path: Path) -> None:
    root = tmp_path / "profiles"
    with store(root).acquire("artifact-only") as lease:
        saved = lease.save(archive(tmp_path, "artifact.tar", b"browser-profile-files"))
    manifest = json.loads((saved.path / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest) == {
        "archive_sha256",
        "created_at",
        "generation",
        "image_version",
        "manifest_version",
        "schema_version",
    }
