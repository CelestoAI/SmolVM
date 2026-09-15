"""Crash-safe storage for dedicated browser profile artifacts.

This module is intentionally not wired into browser sessions yet.  It stores a
profile archive as immutable generations and switches the active generation by
atomically replacing a small ``CURRENT`` pointer.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from smolvm.exceptions import SmolVMError

_PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_MANIFEST_FILE = "manifest.json"
_ARCHIVE_FILE = "profile.tar"
_CURRENT_FILE = "CURRENT"


class BrowserProfileError(SmolVMError):
    """Base error for the internal browser profile artifact store."""


class BrowserProfileLockedError(BrowserProfileError):
    """Raised when another process owns a profile lease."""


class BrowserProfileCompatibilityError(BrowserProfileError):
    """Raised when a profile needs an explicit reset or migration."""


class BrowserProfileCorruptError(BrowserProfileError):
    """Raised when a profile generation fails integrity validation."""


class BrowserProfileOutcomeUnknownError(BrowserProfileError):
    """Raised when a profile save may have become current but is not crash-durable."""

    def __init__(self, profile_id: str, generation_id: str) -> None:
        self.generation_id = generation_id
        super().__init__(
            f"Browser profile '{profile_id}' may now use generation '{generation_id}', "
            "but the save outcome is unknown. Load the profile before trying again."
        )


@dataclass(frozen=True)
class BrowserProfileManifest:
    """Version and integrity metadata for one immutable generation."""

    manifest_version: int
    schema_version: int
    image_version: str
    generation: str
    archive_sha256: str
    created_at: str


@dataclass(frozen=True)
class BrowserProfileGeneration:
    """A validated profile generation and its archive path."""

    manifest: BrowserProfileManifest
    archive_path: Path
    path: Path


class BrowserProfileStore:
    """Own dedicated versioned browser profile artifacts under one directory."""

    def __init__(self, root: Path, *, schema_version: int, image_version: str) -> None:
        if schema_version < 1:
            raise ValueError("schema_version must be at least 1")
        if not image_version.strip():
            raise ValueError("image_version cannot be empty")
        self.root = root
        self.schema_version = schema_version
        self.image_version = image_version

    def acquire(self, profile_id: str) -> BrowserProfileLease:
        """Acquire an exclusive, nonblocking lease for *profile_id*."""

        _validate_profile_id(profile_id)
        _secure_directory(self.root)
        profile_dir = self.root / profile_id
        _secure_directory(profile_dir)
        _secure_directory(profile_dir / "generations")
        lock_path = profile_dir / ".lock"
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise BrowserProfileError(
                "Browser profile locking is unavailable on this host. "
                "Use a POSIX host before enabling persistent browser profiles."
            )
        try:
            lock_fd = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | nofollow,
                0o600,
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise BrowserProfileError(
                    f"Browser profile '{profile_id}' lock cannot be a symbolic link. "
                    "Remove it and try again."
                ) from exc
            raise
        lock_stat = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_stat.st_mode):
            os.close(lock_fd)
            raise BrowserProfileError(
                f"Browser profile '{profile_id}' lock is not a regular file. "
                "Remove it and try again."
            )
        if hasattr(os, "getuid") and lock_stat.st_uid != os.getuid():
            os.close(lock_fd)
            raise BrowserProfileError(
                f"Browser profile '{profile_id}' lock is not owned by the current user. "
                "Choose a profile directory owned by the current user."
            )
        os.fchmod(lock_fd, 0o600)
        try:
            _lock_nonblocking(lock_fd)
        except BlockingIOError as exc:
            os.close(lock_fd)
            raise BrowserProfileLockedError(
                f"Browser profile '{profile_id}' is already in use. "
                "Close its active browser session and try again."
            ) from exc
        except BaseException:
            os.close(lock_fd)
            raise
        return BrowserProfileLease(self, profile_id, profile_dir, lock_fd)


class BrowserProfileLease:
    """Exclusive access to one profile; use as a context manager."""

    def __init__(
        self,
        store: BrowserProfileStore,
        profile_id: str,
        profile_dir: Path,
        lock_fd: int,
    ) -> None:
        self._store = store
        self.profile_id = profile_id
        self.profile_dir = profile_dir
        self._lock_fd: int | None = lock_fd

    def __enter__(self) -> BrowserProfileLease:
        self._assert_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Release this profile lease."""

        if self._lock_fd is None:
            return
        descriptor = self._lock_fd
        self._lock_fd = None
        try:
            _unlock(descriptor)
        finally:
            os.close(descriptor)

    def load(self) -> BrowserProfileGeneration | None:
        """Load and validate the current compatible generation, if any."""

        generation = self._load_current()
        if generation is None:
            return None
        manifest = generation.manifest
        if (
            manifest.schema_version != self._store.schema_version
            or manifest.image_version != self._store.image_version
        ):
            raise BrowserProfileCompatibilityError(
                f"Browser profile '{self.profile_id}' uses schema "
                f"{manifest.schema_version} and image '{manifest.image_version}'. Call reset() or "
                f"migrate() before using schema {self._store.schema_version} and image "
                f"'{self._store.image_version}'."
            )
        return generation

    def save(self, archive: Path) -> BrowserProfileGeneration:
        """Save *archive* as a new fsynced generation and activate it atomically."""

        self._assert_open()
        previous_generation_id = self._current_generation_id()
        self._prune_generations(
            {previous_generation_id} if previous_generation_id is not None else set()
        )
        generation_id = uuid.uuid4().hex
        staged = self.profile_dir / f".staging-{generation_id}"
        _secure_directory(staged)
        try:
            archive_digest = _copy_fsynced(archive, staged / _ARCHIVE_FILE)
        except BaseException:
            shutil.rmtree(staged, ignore_errors=True)
            raise
        manifest = BrowserProfileManifest(
            manifest_version=1,
            schema_version=self._store.schema_version,
            image_version=self._store.image_version,
            generation=generation_id,
            archive_sha256=archive_digest,
            created_at=datetime.now(UTC).isoformat(),
        )
        generations_dir = self.profile_dir / "generations"
        final = generations_dir / generation_id
        try:
            encoded_manifest = (
                json.dumps(asdict(manifest), sort_keys=True, indent=2) + "\n"
            ).encode()
            _write_fsynced(staged / _MANIFEST_FILE, encoded_manifest)
            _fsync_directory(staged)
            os.replace(staged, final)
            _fsync_directory(generations_dir)
        except BaseException:
            shutil.rmtree(staged, ignore_errors=True)
            raise

        pointer_temp = self.profile_dir / f".{_CURRENT_FILE}-{generation_id}"
        try:
            _write_fsynced(pointer_temp, f"{generation_id}\n".encode())
            os.replace(pointer_temp, self.profile_dir / _CURRENT_FILE)
            try:
                _fsync_directory(self.profile_dir)
            except BaseException as exc:
                raise BrowserProfileOutcomeUnknownError(self.profile_id, generation_id) from exc
        finally:
            pointer_temp.unlink(missing_ok=True)
        retained = {generation_id}
        if previous_generation_id is not None:
            retained.add(previous_generation_id)
        self._prune_generations(retained)
        return BrowserProfileGeneration(
            manifest=manifest,
            archive_path=final / _ARCHIVE_FILE,
            path=final,
        )

    def reset(self) -> None:
        """Explicitly remove all generations while this lease is held."""

        self._assert_open()
        current = self.profile_dir / _CURRENT_FILE
        current.unlink(missing_ok=True)
        _fsync_directory(self.profile_dir)
        generations = self.profile_dir / "generations"
        for path in generations.iterdir():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        _fsync_directory(generations)

    def migrate(
        self,
        callback: Callable[[Path, Path], None],
    ) -> BrowserProfileGeneration:
        """Transform the current generation and activate a compatible replacement."""

        self._assert_open()
        current = self._load_current()
        if current is None:
            raise BrowserProfileError(
                f"Browser profile '{self.profile_id}' has no generation to migrate. "
                "Create a new profile or call reset()."
            )
        migration_id = uuid.uuid4().hex
        migration_source = self.profile_dir / f".migration-source-{migration_id}.tar"
        migration_path = self.profile_dir / f".migration-output-{migration_id}.tar"
        _copy_fsynced(current.archive_path, migration_source)
        migration_source.chmod(0o400)
        descriptor = os.open(migration_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        try:
            callback(migration_source, migration_path)
            return self.save(migration_path)
        finally:
            migration_source.unlink(missing_ok=True)
            migration_path.unlink(missing_ok=True)

    def _load_current(self) -> BrowserProfileGeneration | None:
        self._assert_open()
        generation_id = self._current_generation_id()
        if generation_id is None:
            return None
        generation_dir = self.profile_dir / "generations" / generation_id
        manifest_path = generation_dir / _MANIFEST_FILE
        archive_path = generation_dir / _ARCHIVE_FILE
        try:
            raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = _parse_manifest(raw_manifest)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BrowserProfileCorruptError(
                f"Browser profile '{self.profile_id}' is incomplete. Call reset() to remove it."
            ) from exc
        if manifest.manifest_version != 1 or manifest.generation != generation_id:
            raise BrowserProfileCorruptError(
                f"Browser profile '{self.profile_id}' has invalid generation metadata. "
                "Call reset() to remove it."
            )
        try:
            actual_digest = _hash_file(archive_path)
        except (OSError, ValueError) as exc:
            raise BrowserProfileCorruptError(
                f"Browser profile '{self.profile_id}' is incomplete. Call reset() to remove it."
            ) from exc
        if not _is_sha256(manifest.archive_sha256) or actual_digest != manifest.archive_sha256:
            raise BrowserProfileCorruptError(
                f"Browser profile '{self.profile_id}' failed its integrity check. "
                "Call reset() to remove it."
            )
        return BrowserProfileGeneration(
            manifest=manifest,
            archive_path=archive_path,
            path=generation_dir,
        )

    def _current_generation_id(self) -> str | None:
        pointer = self.profile_dir / _CURRENT_FILE
        try:
            pointer_stat = pointer.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(pointer_stat.st_mode):
            raise BrowserProfileCorruptError(
                f"Browser profile '{self.profile_id}' has an invalid CURRENT pointer. "
                "Call reset() to remove it."
            )
        try:
            descriptor = _open_regular_file(pointer)
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                generation_id = handle.read(128).strip()
        except (OSError, ValueError, UnicodeError) as exc:
            raise BrowserProfileCorruptError(
                f"Browser profile '{self.profile_id}' has an invalid CURRENT pointer. "
                "Call reset() to remove it."
            ) from exc
        if not re.fullmatch(r"[a-f0-9]{32}", generation_id):
            raise BrowserProfileCorruptError(
                f"Browser profile '{self.profile_id}' has an invalid CURRENT pointer. "
                "Call reset() to remove it."
            )
        return generation_id

    def _prune_generations(self, retained: set[str]) -> None:
        generations = self.profile_dir / "generations"
        changed = False
        for path in generations.iterdir():
            if path.name in retained:
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            changed = True
        if changed:
            _fsync_directory(generations)

    def _assert_open(self) -> None:
        if self._lock_fd is None:
            raise BrowserProfileError(
                f"Browser profile '{self.profile_id}' lease is closed. Acquire it again and retry."
            )


def _validate_profile_id(profile_id: str) -> None:
    if not _PROFILE_ID.fullmatch(profile_id) or profile_id in {".", ".."}:
        raise ValueError(
            "profile_id must be 1-64 letters, numbers, dots, underscores, or hyphens, "
            "and must start with a letter or number"
        )


def _secure_directory(path: Path) -> None:
    if path.is_symlink():
        raise BrowserProfileError(f"Browser profile path '{path}' cannot be a symbolic link.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise BrowserProfileError(f"Browser profile path '{path}' is not a directory.")
    if hasattr(os, "getuid") and path.stat().st_uid != os.getuid():
        raise BrowserProfileError(
            f"Browser profile path '{path}' is not owned by the current user. "
            "Choose a profile directory owned by the current user."
        )
    path.chmod(0o700)


def _write_fsynced(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    path.chmod(0o600)


def _copy_fsynced(source: Path, destination: Path) -> str:
    source_fd = _open_regular_file(source)
    try:
        destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except BaseException:
        os.close(source_fd)
        raise
    digest = hashlib.sha256()
    try:
        with (
            os.fdopen(source_fd, "rb", closefd=False) as source_handle,
            os.fdopen(destination_fd, "wb", closefd=False) as destination_handle,
        ):
            while chunk := source_handle.read(1024 * 1024):
                digest.update(chunk)
                destination_handle.write(chunk)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
    finally:
        os.close(source_fd)
        os.close(destination_fd)
    destination.chmod(0o600)
    return digest.hexdigest()


def _hash_file(path: Path) -> str:
    descriptor = _open_regular_file(path)
    digest = hashlib.sha256()
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _open_regular_file(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError(f"browser profile archive '{path}' must be a regular file")
    return descriptor


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _is_sha256(value: str) -> bool:
    return bool(re.fullmatch(r"[a-f0-9]{64}", value))


def _lock_nonblocking(descriptor: int) -> None:
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - exercised on non-POSIX hosts
        raise BrowserProfileError(
            "Browser profile locking is unavailable on this host. "
            "Use a POSIX host before enabling persistent browser profiles."
        ) from exc
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(descriptor: int) -> None:
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _parse_manifest(value: object) -> BrowserProfileManifest:
    fields = {
        "manifest_version",
        "schema_version",
        "image_version",
        "generation",
        "archive_sha256",
        "created_at",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("browser profile manifest has unexpected fields")
    if (
        not isinstance(value["manifest_version"], int)
        or isinstance(value["manifest_version"], bool)
        or not isinstance(value["schema_version"], int)
        or isinstance(value["schema_version"], bool)
        or not all(
            isinstance(value[field], str)
            for field in ("image_version", "generation", "archive_sha256", "created_at")
        )
    ):
        raise ValueError("browser profile manifest has invalid field types")
    return BrowserProfileManifest(
        manifest_version=value["manifest_version"],
        schema_version=value["schema_version"],
        image_version=value["image_version"],
        generation=value["generation"],
        archive_sha256=value["archive_sha256"],
        created_at=value["created_at"],
    )
