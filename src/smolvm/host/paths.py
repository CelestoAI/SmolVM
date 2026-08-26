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

"""Writable host paths for SmolVM-managed runtime programs."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

SMOLVM_FIRECRACKER_DIR_ENV = "SMOLVM_FIRECRACKER_DIR"

PathLookup = Callable[[str], str | Path | None]


def _normalize_directory(value: str | Path, *, source: str) -> Path:
    """Expand and absolutize one configured directory without creating it."""
    if isinstance(value, str) and not value.strip():
        if source == SMOLVM_FIRECRACKER_DIR_ENV:
            raise ValueError(
                f"{SMOLVM_FIRECRACKER_DIR_ENV} cannot be empty; unset it or set it to a folder."
            )
        raise ValueError("firecracker_dir cannot be empty; choose a folder for Firecracker.")

    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve(strict=False)


def default_firecracker_dir() -> Path:
    """Return the per-user default Firecracker directory at call time."""
    return (Path.home() / ".smolvm" / "bin").resolve(strict=False)


def configured_firecracker_dir(explicit: str | Path | None = None) -> Path | None:
    """Return an explicit or environment-configured directory, if any."""
    if explicit is not None:
        return _normalize_directory(explicit, source="firecracker_dir")

    if SMOLVM_FIRECRACKER_DIR_ENV in os.environ:
        return _normalize_directory(
            os.environ[SMOLVM_FIRECRACKER_DIR_ENV],
            source=SMOLVM_FIRECRACKER_DIR_ENV,
        )
    return None


def resolve_firecracker_dir(explicit: str | Path | None = None) -> Path:
    """Resolve explicit, environment, then per-user default configuration."""
    return configured_firecracker_dir(explicit) or default_firecracker_dir()


def firecracker_candidates(
    explicit: str | Path | None = None,
    *,
    path_lookup: PathLookup = shutil.which,
) -> tuple[Path, ...]:
    """Return Firecracker candidates in deterministic discovery order.

    An explicit or environment-configured directory is authoritative. Without
    one, a system ``PATH`` installation wins over SmolVM's per-user default.
    """
    configured = configured_firecracker_dir(explicit)
    if configured is not None:
        return (configured / "firecracker",)

    candidates: list[Path] = []
    path_binary = path_lookup("firecracker")
    if path_binary is not None:
        candidates.append(Path(path_binary).expanduser().resolve(strict=False))
    candidates.append(default_firecracker_dir() / "firecracker")

    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return tuple(unique)


def find_firecracker(
    explicit: str | Path | None = None,
    *,
    path_lookup: PathLookup = shutil.which,
) -> Path | None:
    """Return the first configured regular executable Firecracker binary."""
    for candidate in firecracker_candidates(explicit, path_lookup=path_lookup):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def directory_is_on_path(directory: str | Path) -> bool:
    """Return whether *directory* is already in the current ``PATH``."""
    target = _normalize_directory(directory, source="firecracker_dir")
    for raw_entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = raw_entry or os.curdir
        try:
            if _normalize_directory(entry, source="PATH entry") == target:
                return True
        except (OSError, RuntimeError, ValueError):
            continue
    return False
