"""Isolated optional-dependency suite; select this directory's pytest config."""

import sys
from pathlib import Path

# Insert only after interpreter startup: src/smolvm/types.py must not shadow
# stdlib types during site initialization. Containers already contain the package.
HERE = Path(__file__).resolve().parent
if not (HERE / "network_policy").is_dir():
    sys.path.insert(0, str(HERE.parents[2] / "src/smolvm"))


def pytest_ignore_collect(collection_path, config):
    # Ordinary SDK pytest must not import the optional mitmproxy dependency.
    if config.rootpath != HERE and collection_path == HERE / "tests":
        return True
    return None
