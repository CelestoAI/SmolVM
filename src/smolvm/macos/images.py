# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Local-only image management for Apple Virtualization.framework guests."""

from __future__ import annotations

import fcntl
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from smolvm.exceptions import ImageError
from smolvm.host.lume import LUME_VERSION, find_lume_binary, pinned_lume_ready
from smolvm.images.manager import resolve_image_dir
from smolvm.macos.lume import LumeDriver
from smolvm.macos.models import LumeVMDetails, MacOSInstallProgress, MacOSInstallRequest
from smolvm.types import MacOSMachineConfig

MACOS_DEFAULT_IMAGE = "macos-latest"
MACOS_MANIFEST_NAME = "smolvm-manifest.json"
_MINIMUM_FREE_BYTES = 50 * 1024**3
_MINIMUM_INSTALL_FREE_BYTES = 25 * 1024**3
_MINIMUM_IPSW_BYTES = 1024**3
_SAFE_IMAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class MacOSImageManifest(BaseModel):
    """Versioned metadata for one locally installed macOS base image."""

    schema_version: Literal[1] = 1
    name: str
    guest_version: str
    guest_build: str | None = None
    source_ipsw: str
    cpu_count: int = Field(ge=1)
    memory_mib: int = Field(ge=1)
    disk_size_bytes: int = Field(ge=1)
    allocated_size_bytes: int = Field(ge=0)
    driver: Literal["lume"] = "lume"
    driver_version: str
    host_arch: str
    created_at: datetime

    model_config = {"frozen": True}


class MacOSImageManager:
    """Build and resolve macOS images that never leave this host."""

    def __init__(self, image_dir: Path | None = None, driver: LumeDriver | None = None) -> None:
        self.image_dir = image_dir or (resolve_image_dir() / "macos")
        if driver is None:
            binary = find_lume_binary()
            if binary is None or not pinned_lume_ready():
                raise ImageError(
                    "The macOS sandbox runtime isn't installed. Run 'smolvm setup --macos', then "
                    "'smolvm doctor --backend vz' to confirm."
                )
            driver = LumeDriver(binary)
        self.driver = driver

    def bundle_path(self, name: str) -> Path:
        if not _SAFE_IMAGE_NAME.fullmatch(name) or name in {".", ".."}:
            raise ImageError(
                f"'{name}' is not a valid macOS image name; use letters, numbers, dots, "
                "underscores, or hyphens."
            )
        return self.image_dir / name

    def manifest_path(self, name: str) -> Path:
        bundle = self.bundle_path(name)
        if bundle.is_symlink():
            raise ImageError(
                f"macOS image path '{bundle}' is a link; remove it, then rebuild the image."
            )
        return bundle / MACOS_MANIFEST_NAME

    @property
    def cached_latest_ipsw_path(self) -> Path:
        """SmolVM-owned retry cache for Apple's latest restore file."""
        return self.image_dir / ".downloads" / "latest.ipsw"

    @staticmethod
    def _is_complete_ipsw(path: Path) -> bool:
        try:
            return (
                path.is_file()
                and path.stat().st_size >= _MINIMUM_IPSW_BYTES
                and zipfile.is_zipfile(path)
            )
        except OSError:
            return False

    def find_cached_latest_ipsw(self) -> Path | None:
        """Find a complete SmolVM or Lume restore download that can be reused."""
        managed = self.cached_latest_ipsw_path
        if self._is_complete_ipsw(managed):
            return managed
        lume_temporary = Path(tempfile.gettempdir()) / "latest.ipsw"
        if self._is_complete_ipsw(lume_temporary):
            return lume_temporary
        return None

    def _adopt_latest_ipsw(self) -> Path | None:
        """Move Lume's completed temporary download into SmolVM's retry cache."""
        existing = self.find_cached_latest_ipsw()
        if existing is None or existing == self.cached_latest_ipsw_path:
            return existing
        destination = self.cached_latest_ipsw_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.parent.chmod(0o700)
        try:
            os.replace(existing, destination)
        except OSError:
            return existing
        destination.chmod(0o600)
        return destination

    @staticmethod
    def _discard_retry_ipsw(path: Path | None) -> None:
        if path is not None:
            path.unlink(missing_ok=True)
            with suppress(OSError):
                path.parent.rmdir()

    def get(self, name: str = MACOS_DEFAULT_IMAGE) -> MacOSImageManifest:
        path = self.manifest_path(name)
        try:
            raw = path.read_text(encoding="utf-8")
            manifest = MacOSImageManifest.model_validate_json(raw)
        except (OSError, ValueError) as exc:
            raise ImageError(
                f"No ready macOS image named '{name}'. Build it with "
                f"'smolvm image build --os macos --ipsw latest -t {name}'."
            ) from exc
        incompatible_host = platform.system() == "Darwin" and (
            manifest.host_arch.lower() not in {"arm64", "aarch64"}
            or platform.machine().lower() not in {"arm64", "aarch64"}
        )
        if manifest.name != name or manifest.driver_version != LUME_VERSION or incompatible_host:
            raise ImageError(
                f"macOS image '{name}' needs rebuilding; run "
                f"'smolvm image build --os macos --ipsw latest -t {name}'."
            )
        return manifest

    def _check_storage(self, *, minimum_free_bytes: int = _MINIMUM_FREE_BYTES) -> None:
        """Require APFS and enough room before starting Apple's large install."""
        self.image_dir.mkdir(parents=True, exist_ok=True)
        try:
            mounted = subprocess.run(
                ["df", "-P", str(self.image_dir)],
                capture_output=True,
                check=False,
                text=True,
                timeout=10,
            )
            lines = mounted.stdout.splitlines()
            device = lines[-1].split()[0] if mounted.returncode == 0 and len(lines) >= 2 else ""
            result = subprocess.run(
                ["diskutil", "info", "-plist", device],
                capture_output=True,
                check=False,
                timeout=10,
            )
            details = plistlib.loads(result.stdout) if result.returncode == 0 else {}
        except (OSError, plistlib.InvalidFileException, subprocess.TimeoutExpired) as exc:
            raise ImageError(
                f"Could not check the image folder '{self.image_dir}'; run "
                "'smolvm doctor --backend vz' to check this Mac."
            ) from exc
        if str(details.get("FilesystemType", "")).lower() != "apfs":
            raise ImageError(
                f"macOS images need an APFS folder; use the default '{resolve_image_dir()}', "
                "then run 'smolvm image build --os macos --ipsw latest -t macos-latest'."
            )
        free = shutil.disk_usage(self.image_dir).free
        if free < minimum_free_bytes:
            free_gib = free // 1024**3
            required_gib = minimum_free_bytes // 1024**3
            raise ImageError(
                f"macOS image preparation needs {required_gib} GiB free, but this folder has "
                f"{free_gib} GiB; free some space, then retry the image build."
            )

    def _write_manifest(
        self,
        *,
        name: str,
        details: LumeVMDetails,
        source_ipsw: str,
    ) -> MacOSImageManifest:
        manifest = MacOSImageManifest(
            name=name,
            guest_version=details.os,
            source_ipsw=source_ipsw,
            cpu_count=details.cpu_count,
            memory_mib=details.memory_size // (1024 * 1024),
            disk_size_bytes=details.disk_size.total,
            allocated_size_bytes=details.disk_size.allocated,
            driver_version=self.driver.version(),
            host_arch=platform.machine(),
            created_at=datetime.now(timezone.utc),
        )
        manifest_path = self.manifest_path(name)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = manifest_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(manifest.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(manifest_path)
        return manifest

    def build(
        self,
        *,
        name: str = MACOS_DEFAULT_IMAGE,
        ipsw: str | Path = "latest",
        log_path: Path | None = None,
        on_progress: Callable[[MacOSInstallProgress], None] | None = None,
    ) -> MacOSImageManifest:
        """Build one base image while serializing concurrent installers."""
        self.bundle_path(name)
        with _exclusive_lock(self.image_dir / f".{name}.build.lock"):
            return self._build_unlocked(
                name=name,
                ipsw=ipsw,
                log_path=log_path,
                on_progress=on_progress,
            )

    def _build_unlocked(
        self,
        *,
        name: str,
        ipsw: str | Path,
        log_path: Path | None,
        on_progress: Callable[[MacOSInstallProgress], None] | None,
    ) -> MacOSImageManifest:
        self.image_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = self.manifest_path(name)
        if manifest_path.exists():
            return self.get(name)
        bundle = self.bundle_path(name)
        if bundle.is_symlink():
            raise ImageError(
                f"macOS image path '{bundle}' is a link; remove it, then retry the build."
            )
        requested_latest = ipsw == "latest"
        if bundle.is_dir():
            try:
                existing_details = self.driver.inspect(name, storage_path=self.image_dir)
            except Exception as exc:
                raise ImageError(
                    f"Existing macOS image files could not be verified; run 'smolvm image rm "
                    f"{name}', then retry the image build."
                ) from exc
            if (
                existing_details.status.lower() != "stopped"
                or existing_details.provisioning_operation is not None
            ):
                raise ImageError(
                    f"Existing macOS image preparation is incomplete; run 'smolvm image rm "
                    f"{name}', then retry the image build."
                )
            bundle.chmod(0o700)
            manifest = self._write_manifest(
                name=name,
                details=existing_details,
                source_ipsw="latest" if requested_latest else str(ipsw),
            )
            if requested_latest:
                self._discard_retry_ipsw(self.find_cached_latest_ipsw())
            if on_progress is not None:
                with suppress(Exception):
                    on_progress(MacOSInstallProgress("complete", 100))
            return manifest
        resolved_ipsw: str | Path = "latest"
        if requested_latest:
            cached_ipsw = self._adopt_latest_ipsw()
            if cached_ipsw is not None:
                resolved_ipsw = cached_ipsw
        else:
            resolved_ipsw = Path(ipsw).expanduser().resolve()
            if resolved_ipsw.suffix.lower() != ".ipsw" or not resolved_ipsw.is_file():
                raise ImageError(
                    f"macOS restore file was not found at '{resolved_ipsw}'; choose an .ipsw "
                    "file from Apple, or use '--ipsw latest'."
                )
        minimum_free = (
            _MINIMUM_FREE_BYTES if resolved_ipsw == "latest" else _MINIMUM_INSTALL_FREE_BYTES
        )
        self._check_storage(minimum_free_bytes=minimum_free)
        resolved_log = log_path or (self.image_dir / f"{name}.build.log")
        request = MacOSInstallRequest(
            name=name,
            storage_path=self.image_dir,
            ipsw=resolved_ipsw,
        )
        install_completed = False
        try:
            self.driver.install_base_image(
                request,
                log_path=resolved_log,
                on_progress=on_progress,
            )
            install_completed = True
            self.bundle_path(name).chmod(0o700)
            details = self.driver.inspect(name, storage_path=self.image_dir)
            manifest = self._write_manifest(
                name=name,
                details=details,
                source_ipsw="latest" if requested_latest else str(resolved_ipsw),
            )
            if requested_latest:
                self._discard_retry_ipsw(self.find_cached_latest_ipsw())
            if on_progress is not None:
                with suppress(Exception):
                    on_progress(MacOSInstallProgress("complete", 100))
            return manifest
        except (Exception, KeyboardInterrupt):
            if requested_latest:
                self._adopt_latest_ipsw()
            manifest_path.unlink(missing_ok=True)
            bundle = self.bundle_path(name)
            if not install_completed and bundle.is_dir():
                shutil.rmtree(bundle, ignore_errors=True)
            raise

    def machine_config(
        self,
        vm_id: str,
        *,
        data_dir: Path,
        name: str = MACOS_DEFAULT_IMAGE,
    ) -> MacOSMachineConfig:
        manifest = self.get(name)
        return MacOSMachineConfig(
            base_image=name,
            manifest_path=self.manifest_path(name),
            bundle_path=data_dir / "macos-vms" / vm_id,
            guest_version=manifest.guest_version,
            guest_build=manifest.guest_build,
            disk_size_mib=manifest.disk_size_bytes // (1024 * 1024),
        )
