"""Capability detection for smolvm-core."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from . import disk, firecracker, network, qmp


@dataclass(frozen=True, slots=True)
class CoreCapabilities:
    """Native helpers available in the installed smolvm-core wheel."""

    networking: bool
    disk_io: bool
    qmp: bool
    firecracker_api: bool

    def as_dict(self) -> dict[str, bool]:
        """Return capabilities as a JSON-friendly dictionary."""

        return asdict(self)


def detect() -> CoreCapabilities:
    """Return the native helpers available in this process."""

    return CoreCapabilities(
        networking=network.available(),
        disk_io=disk.available(),
        qmp=qmp.available(),
        firecracker_api=firecracker.available(),
    )


def available() -> bool:
    """Return True when at least one native helper is available."""

    caps = detect()
    return caps.networking or caps.disk_io or caps.qmp or caps.firecracker_api


__all__ = [
    "CoreCapabilities",
    "available",
    "detect",
]
