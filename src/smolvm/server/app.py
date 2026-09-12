# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""FastAPI application used by the private TypeScript SDK bridge."""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import re
import secrets
import shlex
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager, suppress
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from queue import Empty, Full, Queue
from typing import Literal, cast

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from smolvm.exceptions import HostError, ImageError, OperationTimeoutError, SmolVMError
from smolvm.facade import SmolVM
from smolvm.server.models import (
    CapabilitiesResponse,
    CreateSandboxRequest,
    DesktopResponse,
    DiagnosticsResponse,
    ErrorResponse,
    ExecRequest,
    ExecResponse,
    SandboxResponse,
)

logger = logging.getLogger(__name__)
_MAX_FILE_BYTES = 16 * 1024 * 1024
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _sdk_error(
    status_code: int,
    code: str,
    detail: str,
    *,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    """Return an HTTP error with a stable SDK code outside the human message."""
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers={"X-SmolVM-Error-Code": code, **(headers or {})},
    )


def _runtime_version() -> str:
    try:
        return version("smolvm")
    except PackageNotFoundError:
        return "source-checkout"


def _command_with_context(body: ExecRequest) -> tuple[str, Literal["login", "raw"]]:
    """Apply cwd and command-local environment without interpolating values."""
    if body.cwd is not None and not PurePosixPath(body.cwd).is_absolute():
        raise ValueError("Working directory must be an absolute sandbox path such as '/workspace'.")
    invalid_names = sorted(name for name in body.env if not _ENV_NAME.fullmatch(name))
    if invalid_names:
        raise ValueError(f"Environment variable name is invalid: {invalid_names[0]!r}.")
    if body.cwd is None and not body.env:
        return body.command, body.shell

    pieces: list[str] = []
    if body.cwd is not None:
        pieces.append(f"cd -- {shlex.quote(body.cwd)}")
    environment = " ".join(
        f"{name}={shlex.quote(value)}" for name, value in sorted(body.env.items())
    )
    command = f"env {environment} {body.command}" if environment else body.command
    pieces.append(command)
    return f"sh -c {shlex.quote(' && '.join(pieces))}", "raw"


def create_app(*, auth_token: str | None = None) -> FastAPI:
    """Build an app with an isolated, process-local sandbox inventory.

    ``auth_token`` is required for SDK sessions. Omitting it keeps the
    manually started development server backward compatible.
    """
    sandboxes: dict[str, SmolVM] = {}
    event_subscribers: set[Queue[dict[str, object]]] = set()
    event_lock = threading.Lock()

    def publish(event: dict[str, object]) -> None:
        with event_lock:
            subscribers = list(event_subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except Full:
                with suppress(Empty):
                    subscriber.get_nowait()
                with suppress(Full):
                    subscriber.put_nowait(event)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        yield
        for sandbox in list(sandboxes.values()):
            with suppress(Exception):
                await asyncio.to_thread(sandbox.delete)
            with suppress(Exception):
                sandbox.close()
        sandboxes.clear()

    app = FastAPI(
        title="SmolVM SDK bridge",
        summary="A private local bridge for SmolVM SDK clients.",
        version="1",
        lifespan=lifespan,
    )
    app.state.auth_token = auth_token
    app.state.sandboxes = sandboxes

    if auth_token is not None:

        @app.middleware("http")
        async def authenticate(request: Request, call_next):  # type: ignore[no-untyped-def]
            supplied = request.headers.get("authorization", "")
            expected = f"Bearer {auth_token}"
            if not secrets.compare_digest(supplied, expected):
                return JSONResponse(
                    status_code=401,
                    headers={"X-SmolVM-Error-Code": "bridge_exit"},
                    content={
                        "detail": ("SDK session authentication failed; create a new SmolVM client.")
                    },
                )
            return await call_next(request)

    def resolve(sandbox_id: str) -> SmolVM:
        vm = sandboxes.get(sandbox_id)
        if vm is None:
            raise _sdk_error(
                404,
                "transport_failed",
                (
                    f"Sandbox '{sandbox_id}' is not part of this SDK session; create it again "
                    "with smolvm.sandboxes.create()."
                ),
            )
        return vm

    @app.get("/sdk/v1/capabilities", response_model=CapabilitiesResponse)
    def capabilities() -> CapabilitiesResponse:
        return CapabilitiesResponse()

    @app.get(
        "/sdk/v1/events",
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}}},
    )
    def events() -> StreamingResponse:
        subscriber: Queue[dict[str, object]] = Queue(maxsize=1)

        def stream() -> Iterator[str]:
            with event_lock:
                event_subscribers.add(subscriber)
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        event = subscriber.get(timeout=10)
                        yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
                    except Empty:
                        yield ": keepalive\n\n"
            finally:
                with event_lock:
                    event_subscribers.discard(subscriber)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/sdk/v1/diagnostics", response_model=DiagnosticsResponse)
    def diagnostics() -> DiagnosticsResponse:
        machine = platform.machine().lower()
        supported = (sys.platform.startswith("linux") and machine in {"amd64", "x86_64"}) or (
            sys.platform == "darwin" and machine == "arm64"
        )
        problems = (
            () if supported else ("Runtime support is limited to Linux x64 and macOS arm64.",)
        )
        return DiagnosticsResponse(
            runtime_version=_runtime_version(),
            python_version=platform.python_version(),
            platform=f"{sys.platform}-{platform.machine()}",
            supported=supported,
            problems=problems,
        )

    @app.post(
        "/sandboxes",
        response_model=SandboxResponse,
        status_code=201,
        operation_id="createSandbox",
        responses={400: {"model": ErrorResponse}},
    )
    def create_sandbox(body: CreateSandboxRequest) -> SandboxResponse:
        values = body.model_dump(exclude_none=True, exclude={"network"})
        if body.image is None and body.os is None:
            values["os"] = "ubuntu"
        network = body.network.model_dump()
        if network["mode"] != "open":
            values["internet_settings"] = network
        sandbox: SmolVM | None = None
        downloaded: dict[str, int] = {}

        def on_download(label: str, chunk: int, total: int | None) -> None:
            downloaded[label] = downloaded.get(label, 0) + chunk
            publish(
                {
                    "type": "image.download",
                    "image": label,
                    "receivedBytes": downloaded[label],
                    **({"totalBytes": total} if total is not None else {}),
                }
            )

        try:
            sandbox = SmolVM(**values, on_download=on_download)
            sandbox.start()
        except (ValueError, SmolVMError) as exc:
            if sandbox is not None:
                with suppress(Exception):
                    sandbox.delete()
                with suppress(Exception):
                    sandbox.close()
            code = (
                "image_download_failed"
                if isinstance(exc, ImageError)
                else "backend_unavailable"
                if isinstance(exc, HostError)
                else "sandbox_create_failed"
            )
            raise _sdk_error(
                400,
                code,
                f"Could not create the sandbox: {exc}. Fix the options and try again.",
            ) from exc
        sandboxes[sandbox.vm_id] = sandbox
        return SandboxResponse(id=sandbox.vm_id, status=sandbox.status)

    @app.get("/sandboxes", response_model=list[SandboxResponse], operation_id="listSandboxes")
    def list_sandboxes() -> list[SandboxResponse]:
        result: list[SandboxResponse] = []
        for sandbox_id, vm in sorted(sandboxes.items()):
            vm.refresh()
            result.append(SandboxResponse(id=sandbox_id, status=vm.status))
        return result

    @app.get("/sandboxes/{sandbox_id}", response_model=SandboxResponse, operation_id="getSandbox")
    def get_sandbox(sandbox_id: str) -> SandboxResponse:
        vm = resolve(sandbox_id)
        vm.refresh()
        return SandboxResponse(id=vm.vm_id, status=vm.status)

    @app.get(
        "/sandboxes/{sandbox_id}/desktop",
        response_model=DesktopResponse,
        operation_id="getSandboxDesktop",
    )
    def get_sandbox_desktop(sandbox_id: str) -> DesktopResponse:
        vm = resolve(sandbox_id)
        vm.refresh()
        endpoint = vm.desktop_endpoint
        if endpoint is None:
            raise _sdk_error(
                409,
                "transport_failed",
                f"Sandbox '{sandbox_id}' has no running desktop.",
            )
        return DesktopResponse(
            protocol=endpoint.protocol,
            host=cast(Literal["127.0.0.1", "localhost", "::1"], endpoint.host),
            port=endpoint.port,
            viewer_url=endpoint.viewer_url,
        )

    @app.delete("/sandboxes/{sandbox_id}", status_code=204, operation_id="deleteSandbox")
    def delete_sandbox(sandbox_id: str) -> Response:
        vm = resolve(sandbox_id)
        deleted = False
        try:
            vm.delete()
            deleted = True
        except (ValueError, SmolVMError) as exc:
            raise _sdk_error(
                409,
                "cleanup_failed",
                (
                    f"Sandbox '{sandbox_id}' could not be deleted; call sandbox.delete() again, "
                    "or close the SmolVM client to clean up its complete session."
                ),
            ) from exc
        finally:
            with suppress(Exception):
                vm.close()
        if deleted:
            sandboxes.pop(sandbox_id, None)
        return Response(status_code=204)

    @app.post("/sandboxes/{sandbox_id}/cancel", status_code=204)
    def cancel_sandbox_operation(sandbox_id: str) -> Response:
        """Stop an in-flight operation by deleting its session VM."""
        return delete_sandbox(sandbox_id)

    @app.post(
        "/sandboxes/{sandbox_id}/exec",
        response_model=ExecResponse,
        operation_id="execCommand",
    )
    def exec_command(sandbox_id: str, body: ExecRequest) -> ExecResponse:
        vm = resolve(sandbox_id)
        try:
            command, shell = _command_with_context(body)
            started = time.monotonic()
            result = vm.run(command, body.timeout, shell)
            duration_ms = round((time.monotonic() - started) * 1000)
        except OperationTimeoutError as exc:
            deleted = False
            try:
                vm.delete()
                deleted = True
            except Exception:
                logger.exception("Could not delete timed-out SDK sandbox %s", sandbox_id)
            finally:
                with suppress(Exception):
                    vm.close()
            if deleted:
                sandboxes.pop(sandbox_id, None)
            raise _sdk_error(
                408,
                "command_timeout",
                (
                    f"Command timed out in sandbox '{sandbox_id}'; "
                    + (
                        "the sandbox was deleted to confirm the command stopped. "
                        "Create a new sandbox and retry."
                        if deleted
                        else "deletion could not be confirmed, so close the SmolVM client "
                        "to stop the complete session."
                    )
                ),
                headers={"X-SmolVM-Sandbox-Deleted": str(deleted).lower()},
            ) from exc
        except (ValueError, SmolVMError) as exc:
            raise _sdk_error(
                409,
                "transport_failed",
                (
                    f"Command could not run in sandbox '{sandbox_id}'; create a new sandbox "
                    "if the session is no longer usable."
                ),
            ) from exc
        return ExecResponse(**result.model_dump(), duration_ms=duration_ms)

    @app.put("/sandboxes/{sandbox_id}/files", status_code=204)
    async def write_file(sandbox_id: str, path: str, request: Request) -> Response:
        vm = resolve(sandbox_id)
        if not PurePosixPath(path).is_absolute():
            raise _sdk_error(400, "invalid_path", "Sandbox file path must be absolute.")
        try:
            declared_size = int(request.headers.get("content-length", "0"))
        except ValueError as exc:
            raise _sdk_error(
                400, "transport_failed", "File size header must be an integer."
            ) from exc
        if declared_size > _MAX_FILE_BYTES:
            raise _sdk_error(413, "transport_failed", "File exceeds the 16 MiB SDK limit.")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="smolvm-sdk-upload-", delete=False) as handle:
                temporary = Path(handle.name)
                received = 0
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > _MAX_FILE_BYTES:
                        raise _sdk_error(
                            413, "transport_failed", "File exceeds the 16 MiB SDK limit."
                        )
                    handle.write(chunk)
            await asyncio.to_thread(vm.upload_file, temporary, path)
        except HTTPException:
            raise
        except (ValueError, SmolVMError) as exc:
            raise _sdk_error(
                409,
                "transport_failed",
                (f"Could not write '{path}' in sandbox '{sandbox_id}'; check the path and retry."),
            ) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return Response(status_code=204)

    @app.get(
        "/sandboxes/{sandbox_id}/files",
        response_class=Response,
        responses={
            200: {
                "content": {
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                }
            }
        },
    )
    async def read_file(sandbox_id: str, path: str) -> Response:
        vm = resolve(sandbox_id)
        if not PurePosixPath(path).is_absolute():
            raise _sdk_error(400, "invalid_path", "Sandbox file path must be absolute.")
        temporary: Path | None = None
        try:
            size_result = await asyncio.to_thread(
                vm.run,
                f"stat -c %s -- {shlex.quote(path)}",
                30,
                "raw",
            )
            if size_result.exit_code != 0:
                raise SmolVMError(size_result.stderr.strip() or f"Could not inspect '{path}'.")
            try:
                guest_size = int(size_result.stdout.strip())
            except ValueError as exc:
                raise SmolVMError(f"Could not determine the size of '{path}'.") from exc
            if guest_size > _MAX_FILE_BYTES:
                raise _sdk_error(413, "transport_failed", "File exceeds the 16 MiB SDK limit.")
            with tempfile.NamedTemporaryFile(prefix="smolvm-sdk-download-", delete=False) as handle:
                temporary = Path(handle.name)
            await asyncio.to_thread(vm.download_file, path, temporary)
            if temporary.stat().st_size > _MAX_FILE_BYTES:
                raise _sdk_error(413, "transport_failed", "File exceeds the 16 MiB SDK limit.")
            content = temporary.read_bytes()
        except HTTPException:
            raise
        except (ValueError, SmolVMError) as exc:
            raise _sdk_error(
                409,
                "transport_failed",
                (f"Could not read '{path}' from sandbox '{sandbox_id}'; check the path and retry."),
            ) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return Response(content=content, media_type="application/octet-stream")

    return app
