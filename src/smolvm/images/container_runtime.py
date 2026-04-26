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

"""Container runtime detection for image builds.

SmolVM shells out to a container runtime to build VM rootfs images
(Dockerfile -> tarball -> ext4). Podman is preferred because it is
rootless on Linux and avoids the Docker daemon/group friction. Docker
is supported as a silent fallback for users who already have it set up.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

from smolvm.exceptions import ImageError

RuntimeKind = Literal["podman", "docker"]

_ENV_OVERRIDE = "SMOLVM_CONTAINER_RUNTIME"
_PROBE_TIMEOUT_SECONDS = 10
_RUNTIME_PREFERENCE: tuple[RuntimeKind, ...] = ("podman", "docker")


class _ProbeStatus(Enum):
    OK = "ok"
    NO_BINARY = "no_binary"
    TIMEOUT = "timeout"
    DAEMON_DOWN = "daemon_down"
    PERMISSION_DENIED = "permission_denied"
    MACHINE_NOT_RUNNING = "machine_not_running"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class _ProbeResult:
    kind: RuntimeKind
    binary: Path | None
    status: _ProbeStatus
    stderr: str = ""


@dataclass(frozen=True)
class ContainerRuntime:
    """A reachable container runtime.

    Attributes:
        kind: Either ``"podman"`` or ``"docker"``.
        binary: Absolute path to the runtime executable.
        needs_machine: True when running Podman on a host that uses a
            managed Linux VM (macOS, Windows). Informational only — if
            this object exists the machine is already up.
    """

    kind: RuntimeKind
    binary: Path
    needs_machine: bool


def detect_container_runtime() -> ContainerRuntime | None:
    """Return the first reachable container runtime, or None.

    Detection order: Podman, then Docker. Honours the
    ``SMOLVM_CONTAINER_RUNTIME`` env var if set to ``podman`` or
    ``docker``. The override forces selection of that runtime only;
    if it is missing or unreachable the function returns None rather
    than silently falling back.
    """
    override = _override_kind()
    if override is not None:
        probe = _probe(override)
        return _runtime_from_probe(probe)

    for kind in _RUNTIME_PREFERENCE:
        probe = _probe(kind)
        if probe.status is _ProbeStatus.OK:
            return _runtime_from_probe(probe)
    return None


def diagnose_container_runtime() -> _ProbeResult:
    """Run the full diagnosis used by error messages and the doctor.

    Returns the most informative probe result across the runtimes we
    consider. If Podman is missing but Docker is unreachable, we
    prefer the Docker probe so the recovery hint matches what the user
    actually has installed.
    """
    override = _override_kind()
    if override is not None:
        return _probe(override)

    podman_probe = _probe("podman")
    if podman_probe.status is _ProbeStatus.OK:
        return podman_probe

    docker_probe = _probe("docker")
    if docker_probe.status is _ProbeStatus.OK:
        return docker_probe

    # Neither is OK. Prefer the one that has a binary on PATH so the
    # error message points to a runtime the user actually installed.
    if podman_probe.status is not _ProbeStatus.NO_BINARY:
        return podman_probe
    if docker_probe.status is not _ProbeStatus.NO_BINARY:
        return docker_probe
    return podman_probe


def container_runtime_error(probe: _ProbeResult | None = None) -> ImageError:
    """Build a user-facing ImageError for an unreachable runtime.

    Pass a probe result from :func:`diagnose_container_runtime` to keep
    the error message specific to the user's situation. When no probe
    is given we run one ourselves.
    """
    if probe is None:
        probe = diagnose_container_runtime()

    runtime_label = "Podman" if probe.kind == "podman" else "Docker"

    if probe.status is _ProbeStatus.NO_BINARY:
        override = _override_kind()
        if override is not None:
            other = "podman" if override == "docker" else "docker"
            return ImageError(
                f"SMOLVM_CONTAINER_RUNTIME={override} is set, but "
                f"'{override}' is not installed. Install {runtime_label}, "
                f"or unset SMOLVM_CONTAINER_RUNTIME to use {other}."
            )
        return ImageError(f"SmolVM needs Podman to build images. {_install_hint()}")

    if probe.status is _ProbeStatus.MACHINE_NOT_RUNNING:
        return ImageError(
            "Podman is installed but its Linux VM is not running. "
            "Run 'podman machine init' (first time only), then "
            "'podman machine start'."
        )

    if probe.status is _ProbeStatus.PERMISSION_DENIED:
        return ImageError(
            "This user cannot reach the Docker socket. Switch to rootless "
            "Podman with 'apt-get install podman' (no daemon, no group "
            "needed), or add yourself to the docker group with "
            "'sudo usermod -aG docker $USER' and re-login."
        )

    if probe.status is _ProbeStatus.TIMEOUT:
        return ImageError(
            f"{runtime_label} is installed but did not respond within "
            f"{_PROBE_TIMEOUT_SECONDS} seconds. {_start_hint(probe.kind)}"
        )

    if probe.status is _ProbeStatus.DAEMON_DOWN:
        details = probe.stderr or "unknown error."
        return ImageError(f"{_start_hint(probe.kind)} Original error: {details}")

    details = probe.stderr or "unknown error."
    return ImageError(
        f"{runtime_label} is required to build images, but it could not be "
        f"used successfully. {_install_hint()} Original error: {details}"
    )


@dataclass(frozen=True)
class RuntimeHealth:
    """Doctor-friendly summary of the container runtime state."""

    status: Literal["pass", "warn"]
    detail: str
    fix: str | None


def runtime_health() -> RuntimeHealth:
    """Probe the container runtime and return a doctor-friendly summary.

    Maps each unhealthy probe state to a specific, actionable detail/fix
    pair so ``smolvm doctor`` can surface why image builds will fail
    (e.g. ``podman machine`` stopped) instead of always reporting that
    no runtime is installed.
    """
    probe = diagnose_container_runtime()
    if probe.status is _ProbeStatus.OK and probe.binary is not None:
        return RuntimeHealth(
            status="pass",
            detail=f"{probe.kind} ({probe.binary})",
            fix=None,
        )
    detail, fix = _describe_runtime_problem(probe)
    return RuntimeHealth(status="warn", detail=detail, fix=fix)


def _describe_runtime_problem(probe: _ProbeResult) -> tuple[str, str]:
    """Return (detail, fix) for an unhealthy probe.

    Used by the doctor to populate ``DoctorCheck.detail`` and
    ``DoctorCheck.fix`` separately, so users can see *what* is wrong and
    *how* to fix it without parsing a single combined sentence.
    """
    label = "Podman" if probe.kind == "podman" else "Docker"

    if probe.status is _ProbeStatus.NO_BINARY:
        override = _override_kind()
        if override is not None:
            other = "podman" if override == "docker" else "docker"
            return (
                f"SMOLVM_CONTAINER_RUNTIME={override} is set, but "
                f"'{override}' is not installed. Image builds will fail.",
                f"Install {label}, or unset SMOLVM_CONTAINER_RUNTIME to use {other}.",
            )
        return (
            "No container runtime found. Image builds will fail.",
            _install_hint(),
        )

    if probe.status is _ProbeStatus.MACHINE_NOT_RUNNING:
        return (
            "Podman is installed but its Linux VM is not running.",
            "Run 'podman machine init' (first time only), then 'podman machine start'.",
        )

    if probe.status is _ProbeStatus.PERMISSION_DENIED:
        return (
            f"This user cannot reach the {label} socket.",
            "Switch to rootless Podman with 'apt-get install -y podman', or add "
            "yourself to the docker group with 'sudo usermod -aG docker $USER' "
            "and re-login.",
        )

    if probe.status is _ProbeStatus.TIMEOUT:
        return (
            f"{label} did not respond within {_PROBE_TIMEOUT_SECONDS} seconds.",
            f"Restart {label} and try again.",
        )

    if probe.status is _ProbeStatus.DAEMON_DOWN:
        if probe.kind == "podman":
            if platform.system() in ("Darwin", "Windows"):
                return (
                    "Podman is installed but its Linux VM is not running.",
                    "Run 'podman machine start'.",
                )
            return (
                "Podman is installed but its user service is not running.",
                "Run 'systemctl --user start podman.socket'.",
            )
        return (
            "Docker is installed but its daemon is not running.",
            "Start Docker Desktop or the Docker service.",
        )

    detail = f"{label} is installed but could not be used."
    if probe.stderr:
        detail = f"{detail} Original error: {probe.stderr}"
    return (detail, _install_hint())


def _override_kind() -> RuntimeKind | None:
    raw = os.environ.get(_ENV_OVERRIDE, "").strip().lower()
    if raw in ("podman", "docker"):
        return raw  # type: ignore[return-value]
    return None


def _runtime_from_probe(probe: _ProbeResult) -> ContainerRuntime | None:
    if probe.status is not _ProbeStatus.OK or probe.binary is None:
        return None
    return ContainerRuntime(
        kind=probe.kind,
        binary=probe.binary,
        needs_machine=probe.kind == "podman" and platform.system() in ("Darwin", "Windows"),
    )


def _probe(kind: RuntimeKind) -> _ProbeResult:
    binary = shutil.which(kind)
    if binary is None:
        return _ProbeResult(kind=kind, binary=None, status=_ProbeStatus.NO_BINARY)

    binary_path = Path(binary)
    try:
        subprocess.run(
            [binary, "info"],
            check=True,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return _ProbeResult(kind=kind, binary=None, status=_ProbeStatus.NO_BINARY)
    except subprocess.TimeoutExpired:
        return _ProbeResult(kind=kind, binary=binary_path, status=_ProbeStatus.TIMEOUT)
    except subprocess.CalledProcessError as exc:
        stderr = "\n".join(part.strip() for part in (exc.stderr, exc.stdout) if part).strip()
        status = _classify_failure(kind, stderr)
        return _ProbeResult(kind=kind, binary=binary_path, status=status, stderr=stderr)

    return _ProbeResult(kind=kind, binary=binary_path, status=_ProbeStatus.OK)


def _classify_failure(kind: RuntimeKind, stderr: str) -> _ProbeStatus:
    text = stderr.lower()

    if kind == "podman" and any(
        marker in text
        for marker in (
            "podman machine",
            "is the podman service running",
            "refresh service connection",
        )
    ):
        return _ProbeStatus.MACHINE_NOT_RUNNING

    if "permission denied" in text and ("docker.sock" in text or "podman.sock" in text):
        return _ProbeStatus.PERMISSION_DENIED

    if any(
        marker in text
        for marker in (
            "cannot connect to the docker daemon",
            "is the docker daemon running",
            "error during connect",
            "connection refused",
        )
    ):
        return _ProbeStatus.DAEMON_DOWN

    return _ProbeStatus.UNKNOWN


def _install_hint() -> str:
    system = platform.system()
    if system == "Darwin":
        return (
            "Install with 'brew install podman && podman machine init && "
            "podman machine start', then rerun."
        )
    return "Install with 'apt-get install -y podman' (Linux), then rerun."


def _start_hint(kind: RuntimeKind) -> str:
    if kind == "podman":
        if platform.system() in ("Darwin", "Windows"):
            return (
                "Podman is installed but its Linux VM is not running. "
                "Start it with 'podman machine start' and try again."
            )
        return (
            "Podman is installed but its user service is not running. "
            "Start it with 'systemctl --user start podman.socket' and try again."
        )
    return (
        "Docker is installed, but SmolVM could not reach the Docker daemon. "
        "Start Docker Desktop or the Docker service and try again."
    )
