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

"""Check PyPI for a newer SmolVM release and notify the user.

This runs at CLI startup. It is best-effort: any network, parse, or
filesystem failure is silently ignored so the CLI never breaks because
of a failed version check.
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

DISABLE_ENV = "SMOLVM_DISABLE_VERSION_CHECK"
PYPI_URL = "https://pypi.org/pypi/smolvm/json"
CACHE_TTL_SECONDS = 60 * 60  # 1h
NETWORK_TIMEOUT_SECONDS = 2.0


def _cache_path() -> Path:
    """Return the path used to cache the last-seen PyPI version."""
    return Path.home() / ".smolvm" / ".version_check.json"


def _read_cache() -> tuple[str, float] | None:
    """Return ``(latest_version, checked_at)`` from the cache, or ``None``."""
    path = _cache_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return None
    try:
        data = json.loads(raw)
        latest = str(data["latest"])
        checked_at = float(data["checked_at"])
    except (ValueError, KeyError, TypeError):
        return None
    return latest, checked_at


def _write_cache(latest: str) -> None:
    """Write the latest PyPI version to the on-disk cache (best effort)."""
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"latest": latest, "checked_at": time.time()}),
            encoding="utf-8",
        )
    except OSError:
        # Cache failures are non-fatal.
        pass


def _fetch_latest_from_pypi() -> str | None:
    """Return the latest stable version from PyPI, or ``None`` on failure."""
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=NETWORK_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    try:
        return str(payload["info"]["version"])
    except (KeyError, TypeError):
        return None


def _is_newer(current: str, latest: str) -> bool:
    """Return True if ``latest`` is strictly greater than ``current``."""
    try:
        from packaging.version import InvalidVersion, Version

        try:
            return Version(latest) > Version(current)
        except InvalidVersion:
            return False
    except ImportError:
        # Fallback: naive string compare on dot-separated integer tuples.
        def _parts(v: str) -> tuple[int, ...]:
            out: list[int] = []
            for chunk in v.split("."):
                digits = ""
                for ch in chunk:
                    if ch.isdigit():
                        digits += ch
                    else:
                        break
                if not digits:
                    return tuple(out)
                out.append(int(digits))
            return tuple(out)

        return _parts(latest) > _parts(current)


def _get_current_version() -> str | None:
    """Return the installed smolvm version, or None if it cannot be determined."""
    try:
        return importlib.metadata.version("smolvm")
    except importlib.metadata.PackageNotFoundError:
        return None


def _is_prerelease(version: str) -> bool:
    """Return True if the given version string is a pre-release."""
    try:
        from packaging.version import InvalidVersion, Version

        try:
            return Version(version).is_prerelease
        except InvalidVersion:
            return False
    except ImportError:
        lowered = version.lower()
        return any(marker in lowered for marker in ("a", "b", "rc", "dev"))


def check_for_update(*, force: bool = False) -> str | None:
    """Return the newer PyPI version if one is available, else ``None``.

    Uses a 1-hour on-disk cache so we only hit PyPI at most once per hour
    per user. Set ``force=True`` to bypass the cache.
    """
    current = _get_current_version()
    if current is None:
        return None

    # Pre-releases are typically ahead of the latest PyPI stable, so
    # there's nothing meaningful to nag about.
    if _is_prerelease(current):
        return None

    latest: str | None = None
    if not force:
        cached = _read_cache()
        if cached is not None:
            cached_latest, checked_at = cached
            if time.time() - checked_at < CACHE_TTL_SECONDS:
                latest = cached_latest

    if latest is None:
        latest = _fetch_latest_from_pypi()
        if latest is None:
            return None
        _write_cache(latest)

    if _is_newer(current, latest):
        return latest
    return None


def maybe_print_update_notice(*, json_output: bool = False) -> None:
    """Print an upgrade nag to stderr if a newer PyPI version is available.

    Skipped when:
      * ``$SMOLVM_DISABLE_VERSION_CHECK`` is set
      * ``json_output`` is True (machine-readable mode)
      * stderr is not a TTY (scripts, CI, pipes)
      * running under pytest
    """
    if json_output:
        return
    if os.environ.get(DISABLE_ENV):
        return
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        if not sys.stderr.isatty():
            return
    except (AttributeError, ValueError):
        return

    try:
        latest = check_for_update()
    except Exception as exc:  # defensive: never break the CLI
        logger.debug("version check failed: %s", exc)
        return

    if latest is None:
        return

    current = _get_current_version() or "?"
    message = (
        f"Hey, there is a new version of smolvm ({latest}, you have {current}). "
        "We recommend you upgrade to the latest version.\n"
        f"  Run: pip install --upgrade smolvm\n"
        f"  (silence this with {DISABLE_ENV}=1)\n"
    )
    try:
        sys.stderr.write(message)
        sys.stderr.flush()
    except (OSError, ValueError):
        pass
