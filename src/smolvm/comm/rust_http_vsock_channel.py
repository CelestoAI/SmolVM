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

"""Host-side HTTP-over-vsock transport for the Rust SmolVM guest agent."""

from __future__ import annotations

import http.client
import io
import ipaddress
import json
import logging
import math
import os
import shlex
import socket
import stat
import tarfile
import time
import urllib.parse
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from smolvm.comm.base import CommChannelKind, ShellMode
from smolvm.exceptions import OperationTimeoutError, SmolVMError
from smolvm.types import CommandResult

logger = logging.getLogger(__name__)

SMOLVM_AGENT_PORT = 1024
"""Guest vsock port where the Rust guest agent listens."""

_READY_FAST_POLL_WINDOW = 1.0
_READY_FAST_POLL_INTERVAL = 0.02
_DEFAULT_MAX_AGENT_RESPONSE_BYTES = 1024 * 1024
_DEFAULT_MAX_STREAM_SIZE_BYTES = 256 * 1024 * 1024
_DEFAULT_MAX_TAR_SIZE_BYTES = 512 * 1024 * 1024
_MAX_PORTS_WAIT = 256
_MAX_PORT_WAIT_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class ControlCapabilities:
    """Cached guest-agent capability flags."""

    protocol_version: int
    features: dict[str, Any]
    limits: dict[str, Any]

    def enabled(self, *names: str) -> bool:
        for name in names:
            value = self.features.get(name)
            if value is True:
                return True
            if isinstance(value, str) and value.lower() == "true":
                return True

            value: Any = self.features
            for part in name.split("."):
                if not isinstance(value, dict) or part not in value:
                    value = None
                    break
                value = value[part]
            if value is True:
                return True
            if isinstance(value, str) and value.lower() == "true":
                return True
        return False


def _feature_required_message(feature: str, sandbox_name: str | None) -> str:
    if sandbox_name:
        sandbox = shlex.quote(sandbox_name)
        return (
            f"Sandbox {sandbox} is using an older image that does not support {feature}; "
            f"run `smolvm sandbox delete {sandbox}` and create it again after updating the image."
        )
    return (
        f"This sandbox is using an older image that does not support {feature}; "
        "run `smolvm sandbox list` to find its name, then delete it and create it again "
        "after updating the image."
    )


def _parse_mode_header(value: str) -> int:
    value = value.strip().removeprefix("0o")
    return int(value, 8)


def _parse_size_header(value: str, *, header: str, method: str, path: str) -> int:
    try:
        size = int(value.strip())
    except ValueError as exc:
        raise SmolVMError(
            f"guest agent returned invalid {header} for {method} {path}: {value!r}"
        ) from exc
    if size < 0:
        raise SmolVMError(f"guest agent returned negative {header} for {method} {path}")
    return size


def _feature_required_error(feature: str, *, sandbox_name: str | None = None) -> SmolVMError:
    return SmolVMError(_feature_required_message(feature, sandbox_name))


def _directory_to_tar(source: Path) -> bytes:
    buffer = io.BytesIO()
    try:
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for child in sorted(source.iterdir()):
                _add_tar_path(archive, child, PurePosixPath(child.name))
    except (tarfile.TarError, ValueError) as exc:
        raise SmolVMError(
            "Directory cannot be uploaded because a file path is too long "
            "for the portable tar format."
        ) from exc
    return buffer.getvalue()


def _strip_tar_owner(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def _add_tar_path(archive: tarfile.TarFile, path: Path, arcname: PurePosixPath) -> None:
    if path.is_symlink():
        return
    archive.add(path, arcname=str(arcname), recursive=False, filter=_strip_tar_owner)
    if path.is_dir():
        for child in sorted(path.iterdir()):
            _add_tar_path(archive, child, arcname / child.name)


def _safe_extract_tar(data: bytes, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        for member in archive.getmembers():
            relative = _safe_tar_member_path(member.name)
            if relative is None:
                continue
            target = destination / relative
            if member.isdir():
                _safe_mkdir(destination, target)
                if member.mode:
                    os.chmod(target, stat.S_IMODE(member.mode))
                continue
            if not member.isfile():
                raise SmolVMError(f"Refusing unsupported tar entry: {member.name}")
            parent = target.parent
            _safe_mkdir(destination, parent)
            _reject_symlink_path(destination, target)
            source = archive.extractfile(member)
            if source is None:
                raise SmolVMError(f"Tar entry has no data: {member.name}")
            target.write_bytes(source.read())
            if member.mode:
                os.chmod(target, stat.S_IMODE(member.mode))


def _safe_tar_member_path(name: str) -> Path | None:
    if name.startswith(("/", "\\")):
        raise SmolVMError(f"Refusing absolute tar entry: {name}")
    parts: list[str] = []
    for part in PurePosixPath(name).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            raise SmolVMError(f"Refusing tar entry outside destination: {name}")
        parts.append(part)
    if not parts:
        return None
    return Path(*parts)


def _safe_mkdir(root: Path, target: Path) -> None:
    relative = target.relative_to(root)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise SmolVMError(f"Refusing tar extraction through symlink: {cursor}")
        cursor.mkdir(exist_ok=True)


def _reject_symlink_path(root: Path, target: Path) -> None:
    relative = target.relative_to(root)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise SmolVMError(f"Refusing tar extraction through symlink: {cursor}")


class _SocketHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that uses a caller-supplied connected socket."""

    def __init__(self, open_socket: Callable[[], socket.socket], *, timeout: float) -> None:
        super().__init__("smolvm-guest-agent", timeout=timeout)
        self._open_socket = open_socket

    def connect(self) -> None:
        self.sock = self._open_socket()
        self.sock.settimeout(self.timeout)


class RustHttpVsockChannel:
    """Drive the Rust guest agent over HTTP on a vsock stream.

    The transport opens one HTTP/1.1 connection per request. It supports both
    native host ``AF_VSOCK`` (QEMU on Linux) and Firecracker's host-side Unix
    socket bridge, which expects a ``CONNECT <port>`` line before the vsock
    byte stream begins.
    """

    kind: CommChannelKind = "vsock"

    def __init__(
        self,
        *,
        guest_cid: int | None = None,
        uds_path: str | Path | None = None,
        agent_port: int = SMOLVM_AGENT_PORT,
        connect_timeout: int = 10,
        sandbox_name: str | None = None,
    ) -> None:
        if (guest_cid is None) == (uds_path is None):
            raise ValueError("provide exactly one of guest_cid or uds_path")
        if connect_timeout < 1:
            raise ValueError("connect_timeout must be >= 1")
        self.guest_cid = guest_cid
        self.uds_path = str(uds_path) if uds_path is not None else None
        self.agent_port = agent_port
        self.connect_timeout = connect_timeout
        self.sandbox_name = sandbox_name
        self._ready = False
        self._capabilities: ControlCapabilities | None = None

    @classmethod
    def from_cid(
        cls,
        guest_cid: int,
        *,
        agent_port: int = SMOLVM_AGENT_PORT,
        connect_timeout: int = 10,
        sandbox_name: str | None = None,
    ) -> RustHttpVsockChannel:
        return cls(
            guest_cid=guest_cid,
            agent_port=agent_port,
            connect_timeout=connect_timeout,
            sandbox_name=sandbox_name,
        )

    @classmethod
    def from_uds(
        cls,
        uds_path: str | Path,
        *,
        agent_port: int = SMOLVM_AGENT_PORT,
        connect_timeout: int = 10,
        sandbox_name: str | None = None,
    ) -> RustHttpVsockChannel:
        return cls(
            uds_path=uds_path,
            agent_port=agent_port,
            connect_timeout=connect_timeout,
            sandbox_name=sandbox_name,
        )

    def _open(self) -> socket.socket:
        if self.uds_path is not None:
            return self._open_uds()
        return self._open_vsock()

    def _open_vsock(self) -> socket.socket:
        if not hasattr(socket, "AF_VSOCK"):
            raise SmolVMError(
                "vsock is not available on this host (no AF_VSOCK). "
                "Use the SSH channel, or run on a Linux host with vhost_vsock loaded."
            )
        sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        sock.settimeout(float(self.connect_timeout))
        try:
            sock.connect((self.guest_cid, self.agent_port))
        except OSError:
            sock.close()
            raise
        return sock

    def _open_uds(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(float(self.connect_timeout))
        try:
            sock.connect(self.uds_path)
            sock.sendall(f"CONNECT {self.agent_port}\n".encode())
            ack = self._read_line(sock)
            if not ack.startswith("OK"):
                raise SmolVMError(f"vsock CONNECT handshake failed: {ack!r}")
        except Exception:
            sock.close()
            raise
        return sock

    @staticmethod
    def _read_line(sock: socket.socket) -> str:
        buf = bytearray()
        while not buf.endswith(b"\n"):
            byte = sock.recv(1)
            if not byte:
                break
            buf.extend(byte)
        return buf.decode("utf-8", errors="replace").strip()

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if payload is not None and body is not None:
            raise ValueError("provide payload or body, not both")
        request_body = b"" if payload is None and body is None else body
        if payload is not None:
            request_body = json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json", "Connection": "close"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        elif content_type is not None:
            headers["Content-Type"] = content_type
        conn = _SocketHTTPConnection(
            self._open,
            timeout=float(timeout if timeout is not None else self.connect_timeout),
        )
        try:
            conn.request(method, path, body=request_body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            if resp.status >= 400:
                detail = data.decode("utf-8", errors="replace")
                raise SmolVMError(f"guest agent HTTP {resp.status} for {method} {path}: {detail}")
            decoded = json.loads(data.decode("utf-8"))
            if not isinstance(decoded, dict):
                raise SmolVMError("guest agent returned a non-object JSON response")
            return decoded
        except TimeoutError as exc:
            raise OperationTimeoutError(
                f"guest agent request: {method} {path}", timeout or 0
            ) from exc
        finally:
            conn.close()

    def _request_bytes(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        *,
        content_type: str | None = None,
        max_bytes: int | None = _DEFAULT_MAX_AGENT_RESPONSE_BYTES,
        timeout: float | None = None,
    ) -> tuple[http.client.HTTPResponse, bytes]:
        headers = {"Connection": "close"}
        if content_type:
            headers["Content-Type"] = content_type
        conn = _SocketHTTPConnection(
            self._open,
            timeout=float(timeout if timeout is not None else self.connect_timeout),
        )
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            if max_bytes is not None:
                for header in ("Content-Length", "x-smolvm-file-size"):
                    value = resp.getheader(header)
                    if value is None:
                        continue
                    declared_size = _parse_size_header(
                        value,
                        header=header,
                        method=method,
                        path=path,
                    )
                    if declared_size > max_bytes:
                        raise SmolVMError(
                            f"guest agent response for {method} {path} exceeded {max_bytes} bytes"
                        )
                data = resp.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise SmolVMError(
                        f"guest agent response for {method} {path} exceeded {max_bytes} bytes"
                    )
            else:
                data = resp.read()
            if resp.status >= 400:
                detail = data.decode("utf-8", errors="replace")
                raise SmolVMError(f"guest agent HTTP {resp.status} for {method} {path}: {detail}")
            return resp, data
        except TimeoutError as exc:
            raise OperationTimeoutError(
                f"guest agent request: {method} {path}", timeout or 0
            ) from exc
        finally:
            conn.close()

    @property
    def capabilities(self) -> ControlCapabilities:
        if self._capabilities is None:
            resp = self._request_json("GET", "/capabilities")
            features = resp.get("features") if isinstance(resp.get("features"), dict) else {}
            limits = resp.get("limits") if isinstance(resp.get("limits"), dict) else {}
            protocol_version = int(resp.get("protocol_version", 1))
            self._capabilities = ControlCapabilities(
                protocol_version=protocol_version,
                features=dict(features),
                limits=dict(limits),
            )
        return self._capabilities

    def supports(self, *features: str) -> bool:
        return self.capabilities.enabled(*features)

    def _require_feature(self, feature: str, *capability_names: str) -> None:
        if not self.supports(*capability_names):
            raise _feature_required_error(feature, sandbox_name=self.sandbox_name)

    def _limit_bytes(self, name: str, default: int) -> int:
        value = self.capabilities.limits.get(name)
        try:
            limit = int(value)
        except (TypeError, ValueError):
            return default
        return limit if limit > 0 else default

    def run(
        self,
        command: str,
        timeout: float = 30,
        shell: ShellMode = "login",
    ) -> CommandResult:
        if not command or not command.strip():
            raise ValueError("command cannot be empty")
        if timeout < 1:
            raise ValueError("timeout must be >= 1")
        timeout_seconds = math.ceil(timeout)
        resp = self._request_json(
            "POST",
            "/exec",
            {"command": command, "shell": shell, "timeout_seconds": timeout_seconds},
            timeout=float(timeout_seconds + self.connect_timeout),
        )
        if resp.get("timed_out"):
            raise OperationTimeoutError(f"vsock run: {command}", timeout_seconds)
        if not resp.get("ok"):
            raise SmolVMError(f"guest agent error during run: {resp.get('error', resp)}")
        return CommandResult(
            exit_code=int(resp.get("exit_code", -1)),
            stdout=str(resp.get("stdout", "")),
            stderr=str(resp.get("stderr", "")),
        )

    def sync(self, timeout: float = 10) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be > 0")
        self._require_feature("saving files before shutdown", "sync")
        resp = self._request_json(
            "POST",
            "/sync",
            timeout=float(timeout + self.connect_timeout),
        )
        if not resp.get("ok"):
            raise SmolVMError(f"guest agent error during sync: {resp.get('error', resp)}")

    def put_file(self, local_path: str | Path, remote_path: str) -> None:
        if not remote_path:
            raise ValueError("remote_path cannot be empty")
        source = Path(local_path)
        if not source.exists():
            raise ValueError(f"local_path does not exist: {source}")
        if not source.is_file():
            raise ValueError(f"local_path is not a file: {source}")
        source_stat = source.stat()
        mode = stat.S_IMODE(source_stat.st_mode)
        self._require_feature("fast file transfer", "file_raw", "files.stream")
        max_stream_size = self._limit_bytes(
            "max_stream_size_bytes",
            _DEFAULT_MAX_STREAM_SIZE_BYTES,
        )
        if source_stat.st_size > max_stream_size:
            raise SmolVMError(
                f"File '{source}' is {source_stat.st_size} bytes, "
                f"but this sandbox accepts files up to {max_stream_size} bytes in one upload."
            )
        query = urllib.parse.urlencode({"path": remote_path, "name": source.name, "mode": mode})
        _resp, data = self._request_bytes(
            "PUT",
            f"/files/content?{query}",
            source.read_bytes(),
            content_type="application/octet-stream",
        )
        decoded = json.loads(data.decode("utf-8")) if data else {}
        if not decoded.get("ok"):
            raise SmolVMError(
                f"Failed to upload file to guest '{remote_path}': {decoded.get('error')}"
            )

    def get_file(self, remote_path: str, local_path: str | Path) -> Path:
        if not remote_path:
            raise ValueError("remote_path cannot be empty")
        destination = Path(local_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._require_feature("fast file transfer", "file_raw", "files.stream")
        query = urllib.parse.urlencode({"path": remote_path})
        resp, data = self._request_bytes(
            "GET",
            f"/files/content?{query}",
            max_bytes=self._limit_bytes(
                "max_stream_size_bytes",
                _DEFAULT_MAX_STREAM_SIZE_BYTES,
            ),
        )
        expected_size = resp.getheader("x-smolvm-file-size")
        if expected_size is not None and int(expected_size) != len(data):
            raise SmolVMError(
                f"Guest file response for '{remote_path}' had size {expected_size}, "
                f"got {len(data)} bytes"
            )
        destination.write_bytes(data)
        mode = resp.getheader("x-smolvm-file-mode")
        if mode is not None:
            os.chmod(destination, _parse_mode_header(mode))
        return destination

    def put_directory(self, local_path: str | Path, remote_path: str) -> None:
        """Upload a directory as a tar stream when the guest supports it."""
        if not remote_path:
            raise ValueError("remote_path cannot be empty")
        source = Path(local_path)
        if not source.is_dir():
            raise ValueError(f"local_path is not a directory: {source}")
        self._require_feature("directory transfer", "dir_tar", "files.directory_tar")
        data = _directory_to_tar(source)
        query = urllib.parse.urlencode({"path": remote_path})
        _resp, response_data = self._request_bytes(
            "PUT",
            f"/directories/tar?{query}",
            data,
            content_type="application/x-tar",
        )
        decoded = json.loads(response_data.decode("utf-8")) if response_data else {}
        if not decoded.get("ok"):
            raise SmolVMError(
                f"Failed to upload directory to guest '{remote_path}': {decoded.get('error')}"
            )

    def get_directory(self, remote_path: str, local_path: str | Path) -> Path:
        """Download a directory tar stream when the guest supports it."""
        if not remote_path:
            raise ValueError("remote_path cannot be empty")
        self._require_feature("directory transfer", "dir_tar", "files.directory_tar")
        destination = Path(local_path)
        destination.mkdir(parents=True, exist_ok=True)
        query = urllib.parse.urlencode({"path": remote_path})
        _resp, data = self._request_bytes(
            "GET",
            f"/directories/tar?{query}",
            max_bytes=self._limit_bytes("max_tar_size_bytes", _DEFAULT_MAX_TAR_SIZE_BYTES),
        )
        _safe_extract_tar(data, destination)
        return destination

    def set_managed_env(self, env_vars: dict[str, str], *, merge: bool = True) -> dict[str, str]:
        self._require_feature("managed environment variables", "env_managed", "env.managed")
        resp = self._request_json("PUT", "/env", {"vars": env_vars, "merge": merge})
        if not resp.get("ok"):
            raise SmolVMError(f"guest agent error during env update: {resp.get('error', resp)}")
        vars_value = resp.get("vars")
        return dict(vars_value) if isinstance(vars_value, dict) else {}

    def unset_managed_env(self, keys: list[str]) -> dict[str, str]:
        self._require_feature("managed environment variables", "env_managed", "env.managed")
        resp = self._request_json("DELETE", "/env", {"keys": keys})
        if not resp.get("ok"):
            raise SmolVMError(f"guest agent error during env update: {resp.get('error', resp)}")
        vars_value = resp.get("vars")
        return dict(vars_value) if isinstance(vars_value, dict) else {}

    def list_managed_env(self) -> dict[str, str]:
        self._require_feature("managed environment variables", "env_managed", "env.managed")
        resp = self._request_json("GET", "/env")
        if not resp.get("ok"):
            raise SmolVMError(f"guest agent error during env read: {resp.get('error', resp)}")
        vars_value = resp.get("vars")
        return dict(vars_value) if isinstance(vars_value, dict) else {}

    def set_env_vars(self, env_vars: dict[str, str], *, merge: bool = True) -> list[str]:
        if not env_vars:
            return []
        return sorted(self.set_managed_env(env_vars, merge=merge))

    def unset_env_vars(self, keys: list[str]) -> dict[str, str]:
        if not keys:
            return {}
        before = self.list_managed_env()
        after = self.unset_managed_env(keys)
        return {key: before[key] for key in keys if key in before and key not in after}

    def list_env_vars(self) -> dict[str, str]:
        return self.list_managed_env()

    def wait_for_ports(
        self,
        ports: list[int],
        *,
        timeout: float = 30,
        host: str = "127.0.0.1",
    ) -> list[int]:
        if not ports:
            raise ValueError("ports cannot be empty")
        if len(ports) > _MAX_PORTS_WAIT:
            raise ValueError(f"ports cannot contain more than {_MAX_PORTS_WAIT} entries")
        invalid_ports = [
            port for port in ports if not isinstance(port, int) or port < 1 or port > 65535
        ]
        if invalid_ports:
            raise ValueError("ports must be integers between 1 and 65535")
        if timeout <= 0:
            raise ValueError("timeout must be > 0")
        if timeout > _MAX_PORT_WAIT_TIMEOUT_SECONDS:
            raise ValueError(f"timeout must be <= {_MAX_PORT_WAIT_TIMEOUT_SECONDS:g} seconds")
        try:
            ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("host must be a valid IP address") from exc
        resp = self._request_json(
            "POST",
            "/ports/wait",
            {"ports": ports, "timeout_ms": int(timeout * 1000), "host": host},
            timeout=timeout + self.connect_timeout,
        )
        if not resp.get("ok"):
            error = str(resp.get("error") or "")
            if error and "timed out" not in error.lower():
                raise SmolVMError(f"guest agent error during port wait: {error}")
            raise OperationTimeoutError(
                f"waiting for guest ports {', '.join(map(str, ports))}", timeout
            )
        ready = resp.get("ready_ports")
        return [int(port) for port in ready] if isinstance(ready, list) else []

    def boot_milestones(self) -> list[dict[str, Any]]:
        resp = self._request_json("GET", "/boot/milestones")
        if not resp.get("ok"):
            raise SmolVMError(
                f"guest agent error during boot milestone read: {resp.get('error', resp)}"
            )
        milestones = resp.get("milestones")
        return list(milestones) if isinstance(milestones, list) else []

    def wait_ready(self, timeout: float = 60.0, interval: float = 0.1) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be > 0")
        if interval <= 0:
            raise ValueError("interval must be > 0")
        started_at = time.monotonic()
        deadline = time.monotonic() + timeout
        last_error = ""
        target = self.uds_path if self.uds_path is not None else f"cid={self.guest_cid}"
        logger.info("Waiting for Rust guest agent on vsock %s (timeout=%.0fs)", target, timeout)

        while time.monotonic() < deadline:
            elapsed = time.monotonic() - started_at
            poll_interval = interval
            if elapsed < _READY_FAST_POLL_WINDOW:
                poll_interval = min(interval, _READY_FAST_POLL_INTERVAL)
            try:
                resp = self._request_json("GET", "/health", timeout=max(1.0, poll_interval))
                if resp.get("status") == "ok":
                    self._ready = True
                    with suppress(Exception):
                        self._capabilities = None
                        _ = self.capabilities
                    logger.info("Rust guest agent is ready on vsock %s", target)
                    return
                last_error = str(resp)
            except (OSError, SmolVMError, ValueError, OperationTimeoutError) as exc:
                last_error = str(exc)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval, remaining))

        raise OperationTimeoutError(
            f"wait_ready(rust-vsock {target}): last error: {last_error}", timeout
        )

    def close(self) -> None:
        self._ready = False
        self._capabilities = None

    @property
    def connected(self) -> bool:
        return self._ready
