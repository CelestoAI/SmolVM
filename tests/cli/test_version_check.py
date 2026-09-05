# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for the SmolVM CLI PyPI version check."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from smolvm.cli import version_check


@pytest.fixture
def home_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``Path.home()`` to an isolated tmp directory."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


class TestIsNewer:
    def test_strictly_greater(self) -> None:
        assert version_check._is_newer("0.0.10", "0.0.11") is True

    def test_equal(self) -> None:
        assert version_check._is_newer("0.0.10", "0.0.10") is False

    def test_older(self) -> None:
        assert version_check._is_newer("0.1.0", "0.0.9") is False

    def test_multi_component(self) -> None:
        assert version_check._is_newer("0.0.10", "0.1.0") is True


class TestCheckForUpdate:
    def test_returns_newer_version(self, home_dir: Path) -> None:
        with (
            patch.object(version_check, "_get_current_version", return_value="0.0.10"),
            patch.object(version_check, "_fetch_latest_from_pypi", return_value="0.1.0"),
        ):
            assert version_check.check_for_update(force=True) == "0.1.0"

    def test_returns_none_when_current(self, home_dir: Path) -> None:
        with (
            patch.object(version_check, "_get_current_version", return_value="0.1.0"),
            patch.object(version_check, "_fetch_latest_from_pypi", return_value="0.1.0"),
        ):
            assert version_check.check_for_update(force=True) is None

    def test_skips_prerelease_current(self, home_dir: Path) -> None:
        """Pre-release installs aren't nagged — they're usually ahead of stable."""
        with (
            patch.object(version_check, "_get_current_version", return_value="0.1.0a1"),
            patch.object(version_check, "_fetch_latest_from_pypi") as fetch,
        ):
            assert version_check.check_for_update(force=True) is None
            fetch.assert_not_called()

    def test_network_failure_returns_none(self, home_dir: Path) -> None:
        with (
            patch.object(version_check, "_get_current_version", return_value="0.0.10"),
            patch.object(version_check, "_fetch_latest_from_pypi", return_value=None),
        ):
            assert version_check.check_for_update(force=True) is None

    def test_uses_fresh_cache(self, home_dir: Path) -> None:
        """A fresh cache entry short-circuits the PyPI call."""
        cache = home_dir / ".smolvm" / ".version_check.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"latest": "0.2.0", "checked_at": time.time()}))

        with (
            patch.object(version_check, "_get_current_version", return_value="0.0.10"),
            patch.object(version_check, "_fetch_latest_from_pypi") as fetch,
        ):
            assert version_check.check_for_update() == "0.2.0"
            fetch.assert_not_called()

    def test_stale_cache_is_refreshed(self, home_dir: Path) -> None:
        cache = home_dir / ".smolvm" / ".version_check.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        stale_ts = time.time() - (version_check.CACHE_TTL_SECONDS + 1)
        cache.write_text(json.dumps({"latest": "0.0.5", "checked_at": stale_ts}))

        with (
            patch.object(version_check, "_get_current_version", return_value="0.0.10"),
            patch.object(version_check, "_fetch_latest_from_pypi", return_value="0.3.0") as fetch,
        ):
            assert version_check.check_for_update() == "0.3.0"
            fetch.assert_called_once()

        # Cache is rewritten with the new value.
        payload = json.loads(cache.read_text())
        assert payload["latest"] == "0.3.0"


class TestMaybePrintUpdateNotice:
    def _force_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stderr.isatty", lambda: True, raising=False)

    def test_prints_when_newer(
        self,
        home_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("SMOLVM_DISABLE_VERSION_CHECK", raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        self._force_tty(monkeypatch)

        with (
            patch.object(version_check, "check_for_update", return_value="0.1.0"),
            patch.object(version_check, "_get_current_version", return_value="0.0.10"),
        ):
            version_check.maybe_print_update_notice(json_output=False)

        err = capsys.readouterr().err
        assert "new version" in err
        assert "0.1.0" in err
        assert "0.0.10" in err
        assert "upgrade" in err.lower()

    def test_silent_when_up_to_date(
        self,
        home_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("SMOLVM_DISABLE_VERSION_CHECK", raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        self._force_tty(monkeypatch)

        with patch.object(version_check, "check_for_update", return_value=None):
            version_check.maybe_print_update_notice(json_output=False)

        assert capsys.readouterr().err == ""

    def test_skipped_in_json_mode(
        self,
        home_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("SMOLVM_DISABLE_VERSION_CHECK", raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        self._force_tty(monkeypatch)

        with patch.object(version_check, "check_for_update") as check:
            version_check.maybe_print_update_notice(json_output=True)
            check.assert_not_called()

        assert capsys.readouterr().err == ""

    def test_skipped_when_env_disabled(
        self,
        home_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("SMOLVM_DISABLE_VERSION_CHECK", "1")
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        self._force_tty(monkeypatch)

        with patch.object(version_check, "check_for_update") as check:
            version_check.maybe_print_update_notice(json_output=False)
            check.assert_not_called()

        assert capsys.readouterr().err == ""

    def test_skipped_when_not_tty(
        self,
        home_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("SMOLVM_DISABLE_VERSION_CHECK", raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.setattr("sys.stderr.isatty", lambda: False, raising=False)

        with patch.object(version_check, "check_for_update") as check:
            version_check.maybe_print_update_notice(json_output=False)
            check.assert_not_called()

        assert capsys.readouterr().err == ""

    def test_skipped_under_pytest(
        self,
        home_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("SMOLVM_DISABLE_VERSION_CHECK", raising=False)
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_foo (call)")
        self._force_tty(monkeypatch)

        with patch.object(version_check, "check_for_update") as check:
            version_check.maybe_print_update_notice(json_output=False)
            check.assert_not_called()

        assert capsys.readouterr().err == ""

    def test_exception_in_check_is_swallowed(
        self,
        home_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("SMOLVM_DISABLE_VERSION_CHECK", raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        self._force_tty(monkeypatch)

        with patch.object(
            version_check,
            "check_for_update",
            side_effect=RuntimeError("boom"),
        ):
            # Must not raise.
            version_check.maybe_print_update_notice(json_output=False)

        assert capsys.readouterr().err == ""
