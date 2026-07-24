# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Pinned Lume 0.4 subprocess adapter for macOS desktop VMs."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlparse

from pydantic import ValidationError

from smolvm.exceptions import SmolVMError
from smolvm.host.lume import LUME_VERSION, lume_version
from smolvm.macos.models import (
    LumeVMDetails,
    MacOSInstallProgress,
    MacOSInstallRequest,
    MacOSLaunchResult,
    MacOSRunRequest,
)
from smolvm.types import DesktopEndpoint

_DOWNLOAD_PROGRESS_RE = re.compile(r"Downloading IPSW Progress:\s*(\d+)%")
_INSTALL_PROGRESS_RE = re.compile(r"Installing macOS progress=(\d+)%")


class LumeDriver:
    """Translate SmolVM-owned operations into the pinned Lume CLI."""

    def __init__(self, binary: Path) -> None:
        self.binary = binary

    @staticmethod
    def _environment(*, log_level: str = "error") -> dict[str, str]:
        return {**os.environ, "LUME_LOG_LEVEL": log_level}

    def version(self) -> str:
        return lume_version(self.binary)

    def ensure_compatible(self) -> None:
        actual = self.version()
        if actual != LUME_VERSION:
            raise SmolVMError(
                f"The macOS sandbox runtime is version {actual!r}, but this SmolVM release "
                f"needs {LUME_VERSION!r}. Run 'smolvm setup --macos' to install the tested version."
            )

    def _run(
        self,
        arguments: list[str],
        *,
        sandbox_name: str,
        timeout: float = 60.0,
    ) -> subprocess.CompletedProcess[str]:
        logs_command = f"smolvm sandbox logs {shlex.quote(sandbox_name)}"
        try:
            result = subprocess.run(
                [str(self.binary), *arguments],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
                env=self._environment(),
            )
        except subprocess.TimeoutExpired as exc:
            raise SmolVMError(
                f"The macOS sandbox runtime did not finish in time; run '{logs_command}' "
                "for details."
            ) from exc
        except OSError as exc:
            raise SmolVMError(
                "The macOS sandbox runtime could not start; run "
                "'smolvm doctor --backend vz' to check this Mac."
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            detail = re.sub(r"vnc://[^\s@]*@", "vnc://<redacted>@", detail)
            if len(detail) > 500:
                detail = f"{detail[:497]}..."
            suffix = f" Runtime detail: {detail}" if detail else ""
            raise SmolVMError(
                f"The macOS sandbox runtime failed; run '{logs_command}' for details, then "
                f"'smolvm doctor --backend vz' to check this Mac.{suffix}"
            )
        return result

    @staticmethod
    def _progress_from_line(line: str) -> MacOSInstallProgress | None:
        if match := _DOWNLOAD_PROGRESS_RE.search(line):
            return MacOSInstallProgress("download", min(int(match.group(1)), 100))
        if match := _INSTALL_PROGRESS_RE.search(line):
            return MacOSInstallProgress("install", min(int(match.group(1)), 100))
        if "Starting macOS installation" in line:
            return MacOSInstallProgress("install", 0)
        if "Starting offline unattended" in line:
            return MacOSInstallProgress("setup")
        return None

    @classmethod
    def _stream_install_output(
        cls,
        stream: BinaryIO,
        log_path: Path,
        on_progress: Callable[[MacOSInstallProgress], None] | None,
    ) -> None:
        try:
            with log_path.open("ab") as log:
                while raw_line := stream.readline():
                    safe_line = re.sub(rb"vnc://[^\s@]*@", b"vnc://<redacted>@", raw_line)
                    log.write(safe_line)
                    log.flush()
                    if on_progress is not None:
                        update = cls._progress_from_line(
                            safe_line.decode("utf-8", errors="replace")
                        )
                        if update is not None:
                            with suppress(Exception):
                                on_progress(update)
        except OSError:
            return

    def install_base_image(
        self,
        request: MacOSInstallRequest,
        *,
        log_path: Path,
        on_progress: Callable[[MacOSInstallProgress], None] | None = None,
    ) -> None:
        """Install a local base image while streaming machine-readable progress."""
        ipsw = request.ipsw if isinstance(request.ipsw, str) else str(request.ipsw)
        arguments = [
            "create",
            request.name,
            "--ipsw",
            ipsw,
            "--unattended",
            request.unattended_preset,
            "--storage",
            str(request.storage_path),
            "--cpu",
            str(request.cpu_count),
            "--memory",
            f"{request.memory_mib}MB",
            "--disk-size",
            f"{request.disk_size_gib}GB",
            "--display",
            f"{request.display_width}x{request.display_height}",
            "--network",
            "nat",
        ]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(mode=0o600, exist_ok=True)
        log_path.chmod(0o600)
        if on_progress is not None:
            initial_phase = "download" if ipsw == "latest" else "install"
            with suppress(Exception):
                on_progress(MacOSInstallProgress(initial_phase, 0))
        try:
            process = subprocess.Popen(
                [str(self.binary), *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=self._environment(log_level="info"),
                start_new_session=True,
            )
        except OSError as exc:
            raise SmolVMError(
                "macOS image preparation did not start; run "
                f"'smolvm image build --os macos --ipsw {ipsw} -t {request.name}' to try again."
            ) from exc
        assert process.stdout is not None
        reader = threading.Thread(
            target=self._stream_install_output,
            args=(process.stdout, log_path, on_progress),
            daemon=True,
            name=f"smolvm-lume-install-{request.name}",
        )
        reader.start()
        try:
            return_code = process.wait(timeout=90 * 60)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if isinstance(exc, KeyboardInterrupt):
                raise
            raise SmolVMError(
                "macOS image preparation did not finish; run "
                f"'smolvm image build --os macos --ipsw {ipsw} -t {request.name}' to try again."
            ) from exc
        finally:
            reader.join(timeout=5)
        if return_code != 0:
            raise SmolVMError(
                "macOS image preparation failed; inspect the image build log, then run "
                f"'smolvm image build --os macos --ipsw {ipsw} -t {request.name}' again."
            )

    def inspect(self, name: str, *, storage_path: Path) -> LumeVMDetails:
        result = self._run(
            ["get", name, "--format", "json", "--storage", str(storage_path)],
            sandbox_name=name,
            timeout=15,
        )
        try:
            payload = json.loads(result.stdout)
            if not isinstance(payload, list) or len(payload) != 1:
                raise ValueError("expected one VM record")
            return LumeVMDetails.model_validate(payload[0])
        except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as exc:
            raise SmolVMError(
                "The macOS sandbox runtime returned data SmolVM could not read; run "
                "'smolvm setup --macos' to install the tested runtime version."
            ) from exc

    def clone(
        self,
        source: str,
        destination: str,
        *,
        source_storage: Path,
        destination_storage: Path,
    ) -> None:
        self._run(
            [
                "clone",
                source,
                destination,
                "--source-storage",
                str(source_storage),
                "--dest-storage",
                str(destination_storage),
            ],
            sandbox_name=destination,
            timeout=15 * 60,
        )

    @staticmethod
    def _share_argument(path: Path, *, writable: bool, sandbox_name: str) -> str:
        value = str(path)
        if ":" in value:
            safe_path = value.replace(":", "-")
            writable_option = " --writable-mounts" if writable else ""
            retry = (
                f"smolvm sandbox create --os macos --name {shlex.quote(sandbox_name)} "
                f"--mount {shlex.quote(safe_path)}{writable_option}"
            )
            raise SmolVMError(
                f"Shared folder path contains ':': '{value}'. Move it to '{safe_path}', then run "
                f"'{retry}'."
            )
        return f"{value}:{'rw' if writable else 'ro'}"

    @staticmethod
    def _display_from_details(details: LumeVMDetails) -> DesktopEndpoint | None:
        if details.vnc_url is None:
            return None
        parsed = urlparse(details.vnc_url)
        if parsed.scheme != "vnc" or parsed.hostname is None:
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        if port is None:
            return None
        try:
            return DesktopEndpoint(host=parsed.hostname, port=port)
        except ValidationError:
            return None

    @staticmethod
    def _write_redacted_log(stream: BinaryIO, log_path: Path) -> None:
        """Drain Lume output while removing credentials from VNC URLs."""
        with log_path.open("ab") as log:
            while chunk := stream.readline():
                log.write(re.sub(rb"vnc://[^\s@]*@", b"vnc://<redacted>@", chunk))
                log.flush()

    @staticmethod
    def _protect_runtime_secrets(storage_path: Path, name: str) -> None:
        for path in (storage_path / name / "sessions.json", storage_path / name / "vnc.env"):
            if path.exists():
                path.chmod(0o600)

    def start(
        self,
        request: MacOSRunRequest,
        *,
        log_path: Path,
        timeout: float,
    ) -> tuple[subprocess.Popen[bytes], MacOSLaunchResult]:
        arguments = [
            "run",
            request.name,
            "--storage",
            str(request.storage_path),
            "--no-display",
            "--vnc-port",
            "0",
            "--network",
            "nat",
        ]
        for mount in request.workspace_mounts:
            arguments.extend(
                [
                    "--shared-dir",
                    self._share_argument(
                        mount.host_path,
                        writable=mount.writable,
                        sandbox_name=request.name,
                    ),
                ]
            )

        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(mode=0o600, exist_ok=True)
        log_path.chmod(0o600)
        try:
            process = subprocess.Popen(
                [str(self.binary), *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=self._environment(),
                start_new_session=True,
            )
            assert process.stdout is not None
            threading.Thread(
                target=self._write_redacted_log,
                args=(process.stdout, log_path),
                daemon=True,
                name=f"smolvm-lume-log-{request.name}",
            ).start()
        except OSError as exc:
            raise SmolVMError(
                "The macOS sandbox runtime could not start; run "
                "'smolvm doctor --backend vz' to check this Mac."
            ) from exc

        logs_command = f"smolvm sandbox logs {shlex.quote(request.name)}"
        deadline = time.monotonic() + timeout
        last_inspect_error: SmolVMError | None = None
        try:
            while time.monotonic() < deadline:
                return_code = process.poll()
                if return_code is not None:
                    raise SmolVMError(
                        f"The macOS sandbox runtime exited with code {return_code}; run "
                        f"'{logs_command}' for details."
                    )
                try:
                    details = self.inspect(request.name, storage_path=request.storage_path)
                except SmolVMError as exc:
                    last_inspect_error = exc
                    time.sleep(0.25)
                    continue
                display = self._display_from_details(details)
                parsed_vnc = urlparse(details.vnc_url) if details.vnc_url else None
                password = parsed_vnc.password if parsed_vnc is not None else None
                if details.status == "running" and display is not None and password:
                    self._protect_runtime_secrets(request.storage_path, request.name)
                    return process, MacOSLaunchResult(
                        pid=process.pid,
                        display=display,
                        ip_address=details.ip_address,
                        vnc_password=password,
                    )
                time.sleep(0.25)
            inspect_detail = (
                f" Last runtime error: {last_inspect_error}" if last_inspect_error else ""
            )
            raise SmolVMError(
                f"The macOS desktop did not become ready in {timeout:g} seconds; run "
                f"'{logs_command}' for details.{inspect_detail}"
            )
        except (Exception, KeyboardInterrupt):
            self._protect_runtime_secrets(request.storage_path, request.name)
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            raise

    def stop(self, name: str, *, storage_path: Path, timeout: float) -> None:
        self._run(
            ["stop", name, "--storage", str(storage_path)],
            sandbox_name=name,
            timeout=timeout + 15,
        )

    def delete(self, name: str, *, storage_path: Path) -> None:
        self._run(
            ["delete", name, "--force", "--storage", str(storage_path)],
            sandbox_name=name,
            timeout=15 * 60,
        )
