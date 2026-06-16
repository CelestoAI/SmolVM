"""Tests for SmolVM version consistency."""

import re

import pytest


class TestVersion:
    """Verify version is consistent across all surfaces."""

    def test_package_version_is_semver(self) -> None:
        """Package version should be a valid semver string."""
        import smolvm

        assert re.match(r"^\d+\.\d+\.\d+", smolvm.__version__), (
            f"Version {smolvm.__version__!r} is not a valid semver"
        )

    def test_init_version_matches_metadata(self) -> None:
        """smolvm.__version__ should match importlib.metadata."""
        import importlib.metadata

        import smolvm

        metadata_version = importlib.metadata.version("smolvm")
        assert smolvm.__version__ == metadata_version

    def test_cli_version_flag(self) -> None:
        """smolvm --version should print the package version."""
        from smolvm.cli.main import build_parser

        parser = build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--version"])
        assert exc_info.value.code == 0

    def test_cli_short_version_flag(self) -> None:
        """smolvm -V should also trigger version output."""
        from smolvm.cli.main import build_parser

        parser = build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["-V"])
        assert exc_info.value.code == 0

    def test_version_not_hardcoded_in_init(self) -> None:
        """__version__ should come from package metadata, not a hardcoded string."""
        import inspect

        import smolvm

        source = inspect.getsource(smolvm)
        assert '__version__ = "' not in source, (
            "__version__ should be read from importlib.metadata, not hardcoded"
        )
