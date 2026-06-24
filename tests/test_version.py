"""Tests for SmolVM version consistency."""

import importlib.metadata
import inspect
import re

import smolvm
from smolvm.cli.main import main


class TestVersion:
    """Verify version is consistent across all surfaces."""

    def test_package_version_is_valid(self) -> None:
        """Package version should be a valid release string."""
        assert re.match(r"^\d+\.\d+\.\d+(?:\.post\d+)?$", smolvm.__version__), (
            f"Version {smolvm.__version__!r} is not a valid release version"
        )

    def test_init_version_matches_metadata(self) -> None:
        """smolvm.__version__ should match importlib.metadata."""
        metadata_version = importlib.metadata.version("smolvm")
        assert smolvm.__version__ == metadata_version

    def test_cli_version_flag(self, capsys) -> None:
        """smolvm --version should print the package version."""
        assert main(["--version"]) == 0
        assert smolvm.__version__ in capsys.readouterr().out

    def test_cli_short_version_flag(self, capsys) -> None:
        """smolvm -V should also trigger version output."""
        assert main(["-V"]) == 0
        assert smolvm.__version__ in capsys.readouterr().out

    def test_version_not_hardcoded_in_init(self) -> None:
        """__version__ should come from package metadata, not a hardcoded string."""
        source = inspect.getsource(smolvm)
        assert '__version__ = "' not in source, (
            "__version__ should be read from importlib.metadata, not hardcoded"
        )
