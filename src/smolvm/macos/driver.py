# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Vendor-neutral protocol for a macOS Virtualization.framework driver."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from smolvm.macos.models import (
    LumeVMDetails,
    MacOSInstallProgress,
    MacOSInstallRequest,
    MacOSLaunchResult,
    MacOSRunRequest,
)


class MacOSRuntimeDriver(Protocol):
    """Operations SmolVM needs from its replaceable macOS runtime helper."""

    def version(self) -> str:
        """Return the runtime driver's version."""

    def install_base_image(
        self,
        request: MacOSInstallRequest,
        *,
        log_path: Path,
        on_progress: Callable[[MacOSInstallProgress], None] | None = None,
    ) -> None:
        """Install a reusable macOS machine from an Apple IPSW."""

    def inspect(self, name: str, *, storage_path: Path) -> LumeVMDetails:
        """Return machine-readable state for one local macOS VM."""

    def clone(
        self,
        source: str,
        destination: str,
        *,
        source_storage: Path,
        destination_storage: Path,
    ) -> None:
        """Clone a stopped local machine."""

    def start(
        self,
        request: MacOSRunRequest,
        *,
        log_path: Path,
        timeout: float,
    ) -> tuple[subprocess.Popen[bytes], MacOSLaunchResult]:
        """Start a VM and return after its desktop is available."""

    def stop(self, name: str, *, storage_path: Path, timeout: float) -> None:
        """Stop a running VM."""

    def delete(self, name: str, *, storage_path: Path) -> None:
        """Delete one stopped VM bundle."""
