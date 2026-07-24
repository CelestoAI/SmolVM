# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Discovery and checksum-verified installation of the pinned Lume driver."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import NamedTuple

from smolvm.exceptions import HostError

LUME_VERSION = "0.4.0"
LUME_SOURCE_COMMIT = "ee15ae942cefe809fd97a565220eca9c6a295ac0"
LUME_ARCHIVE_SHA256 = "8b44bbcc5ae9693f4b1343fea58aadddd37053fa990cd234e703c8c9e73b1cba"
LUME_ARCHIVE_URL = (
    "https://github.com/trycua/cua/releases/download/"
    f"lume-v{LUME_VERSION}/lume-{LUME_VERSION}-darwin-arm64.tar.gz"
)
LUME_BIN_DIR = Path.home() / ".smolvm" / "bin"
LUME_MANAGED_PATH = LUME_BIN_DIR / "lume"
LUME_INSTALL_ROOT = Path.home() / ".smolvm" / "lib" / f"lume-{LUME_VERSION}"
_VERSION_LINE_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$")


class MacOSHostCapabilities(NamedTuple):
    """Apple Silicon and host-version capabilities needed by the VZ backend."""

    is_apple_silicon: bool
    version: str
    major_version: int

    @property
    def supported_version(self) -> bool:
        return self.major_version >= 14


def macos_major_version(version: str | None = None) -> int:
    """Return a macOS major version, or zero when the value is unreadable."""
    value = platform.mac_ver()[0] if version is None else version
    try:
        return int(value.split(".", 1)[0])
    except (ValueError, IndexError):
        return 0


def macos_host_capabilities() -> MacOSHostCapabilities:
    """Return the shared host gates for local macOS desktop sandboxes."""
    version = platform.mac_ver()[0]
    return MacOSHostCapabilities(
        is_apple_silicon=(
            platform.system() == "Darwin" and platform.machine().lower() in {"arm64", "aarch64"}
        ),
        version=version,
        major_version=macos_major_version(version),
    )


def supported_lume_host() -> bool:
    """Return whether this host can run the pinned Lume build."""
    return macos_host_capabilities().is_apple_silicon


def find_lume_binary() -> Path | None:
    """Find the checksum-verified Lume release managed by SmolVM."""
    if LUME_MANAGED_PATH.is_file() and os.access(LUME_MANAGED_PATH, os.X_OK):
        return LUME_MANAGED_PATH
    return None


def lume_version(binary: Path, *, timeout: float = 5.0) -> str:
    """Return the normalized version reported by a Lume executable."""
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HostError(f"Could not run the macOS sandbox runtime: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit code {result.returncode}"
        raise HostError(f"Could not check the macOS sandbox runtime: {detail}")
    lines = [line.strip().removeprefix("lume ").strip() for line in result.stdout.splitlines()]
    versions = [line for line in lines if _VERSION_LINE_RE.fullmatch(line)]
    if not versions:
        raise HostError("The macOS sandbox runtime did not report a readable version.")
    return versions[-1]


def pinned_lume_ready() -> bool:
    """Return whether the exact tested Lume release is installed."""
    binary = find_lume_binary()
    if binary is None:
        return False
    try:
        if binary == LUME_MANAGED_PATH:
            _verify_lume_app(LUME_INSTALL_ROOT / "lume.app")
        return lume_version(binary) == LUME_VERSION
    except HostError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_lume_app(app_path: Path) -> None:
    """Require the signed, notarized app and its virtualization entitlement."""
    checks = (
        (["codesign", "--verify", "--deep", "--strict", str(app_path)], "signature"),
        (["spctl", "--assess", "--type", "execute", str(app_path)], "notarization"),
    )
    for command, label in checks:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HostError(
                f"Could not verify the macOS runtime {label}; run 'smolvm setup --macos' again."
            ) from exc
        if result.returncode != 0:
            raise HostError(
                f"The macOS runtime failed {label} verification; run 'smolvm setup --macos' again."
            )
    entitlements = subprocess.run(
        ["codesign", "-d", "--entitlements", ":-", str(app_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if (
        entitlements.returncode != 0
        or "com.apple.security.virtualization" not in entitlements.stderr + entitlements.stdout
    ):
        raise HostError(
            "The macOS runtime lacks Apple's virtualization permission; run "
            "'smolvm setup --macos' again."
        )


def _extract_lume_bundle(archive: Path, destination: Path) -> None:
    """Safely extract the signed app bundle and its command wrapper."""
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        names = {member.name.rstrip("/") for member in members}
        required = {"lume", "lume.app/Contents/MacOS/lume"}
        if not required.issubset(names):
            raise HostError("The downloaded macOS runtime archive is missing its app bundle.")
        for member in members:
            relative = Path(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise HostError("The downloaded macOS runtime archive contains an unsafe path.")
            if member.issym() or member.islnk() or member.isdev():
                raise HostError("The downloaded macOS runtime archive contains an unsafe link.")
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(member.mode & 0o777)
                continue
            if not member.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            if source is None:
                raise HostError(
                    f"The downloaded macOS runtime file '{member.name}' could not be read."
                )
            with target.open("wb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(member.mode & 0o777)


def install_pinned_lume(*, destination: Path = LUME_MANAGED_PATH) -> Path:
    """Download, verify, and atomically install the tested Lume app bundle."""
    capabilities = macos_host_capabilities()
    if not capabilities.is_apple_silicon:
        raise HostError(
            "macOS sandboxes need an Apple Silicon Mac. Run "
            "'smolvm sandbox create --os alpine' on this machine instead."
        )
    if not capabilities.supported_version:
        raise HostError(
            "macOS desktop sandboxes need macOS 14 or newer. Update this Mac, then run "
            "'smolvm setup --macos' again."
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    install_root = (
        LUME_INSTALL_ROOT
        if destination == LUME_MANAGED_PATH
        else destination.parent / f".lume-{LUME_VERSION}"
    )
    install_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="smolvm-lume-", dir=str(install_root.parent)) as tmp:
        tmp_dir = Path(tmp)
        archive = tmp_dir / "lume.tar.gz"
        staged = tmp_dir / "bundle"
        try:
            urllib.request.urlretrieve(LUME_ARCHIVE_URL, archive)  # noqa: S310 - pinned HTTPS URL
        except OSError as exc:
            raise HostError(
                "Could not download the macOS sandbox runtime. Check your connection, then run "
                "'smolvm setup --macos' again."
            ) from exc
        actual = _sha256(archive)
        if actual != LUME_ARCHIVE_SHA256:
            raise HostError(
                "The downloaded macOS sandbox runtime failed its checksum; run "
                "'smolvm setup --macos' to download it again."
            )
        staged.mkdir()
        _extract_lume_bundle(archive, staged)
        _verify_lume_app(staged / "lume.app")
        if install_root.exists():
            shutil.rmtree(install_root)
        os.replace(staged, install_root)

    app_binary = install_root / "lume.app" / "Contents" / "MacOS" / "lume"
    wrapper = f'#!/bin/sh\nexec "{app_binary}" "$@"\n'
    staged_wrapper = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    staged_wrapper.write_text(wrapper, encoding="utf-8")
    staged_wrapper.chmod(0o755)
    os.replace(staged_wrapper, destination)

    installed_version = lume_version(destination)
    if installed_version != LUME_VERSION:
        destination.unlink(missing_ok=True)
        shutil.rmtree(install_root, ignore_errors=True)
        raise HostError(
            f"The macOS sandbox runtime reported version {installed_version!r}, expected "
            f"{LUME_VERSION!r}; run 'smolvm setup --macos' to reinstall it."
        )
    subprocess.run(
        [str(destination), "config", "telemetry", "disable"],
        capture_output=True,
        check=False,
        timeout=10,
    )
    return destination
