"""Rust-native helpers for SmolVM.

Use this package directly when you are working on SmolVM's native helpers or
checking which fast paths are available on a host. Most applications should use
the main :mod:`smolvm` package instead.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from . import capabilities, disk, errors, firecracker, network, qmp
from .capabilities import CoreCapabilities, detect

try:
    __version__ = version("smolvm-core")
except PackageNotFoundError:  # pragma: no cover - editable tree without metadata
    __version__ = "0+unknown"

__all__ = [
    "CoreCapabilities",
    "capabilities",
    "detect",
    "disk",
    "errors",
    "firecracker",
    "network",
    "qmp",
]
