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
    BrowserSessionResponse,
    CapabilitiesResponse,
    ComputerBrowserResponse,
    ComputerDisplayResponse,
    ComputerResponse,
    CreateBrowserSessionRequest,
    CreateComputerRequest,
    CreateSandboxRequest,
    DesktopResponse,
    DiagnosticsResponse,
    ErrorResponse,
    ExecRequest,
    ExecResponse,
    SandboxResponse,
)
from smolvm.types import ComputerSandboxProtocol, DisplaySandboxProtocol

logger = logging.getLogger(__name__)
_MAX_FILE_BYTES = 16 * 1024 * 1024
_DOWNLOAD_EVENT_INTERVAL_SECONDS = 1.0
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


class _DownloadProgressEvents:
    """Coalesce chunk callbacks into useful human-scale progress events."""

    def __init__(self, interval: float = _DOWNLOAD_EVENT_INTERVAL_SECONDS) -> None:
        self._interval = interval
        self._received: dict[str, int] = {}
        self._last_published: dict[str, float] = {}
        self._completed: set[str] = set()

    def update(
        self,
        label: str,
        chunk: int,
        total: int | None,
        *,
        now: float | None = None,
    ) -> dict[str, object] | None:
        received = self._received.get(label, 0) + chunk
        self._received[label] = received
        if label in self._completed:
            return None
        timestamp = time.monotonic() if now is None else now
        final = total is not None and received >= total
        last_published = self._last_published.get(label)
        if not final and last_published is not None and timestamp - last_published < self._interval:
            return None
        self._last_published[label] = timestamp
        if final:
            self._completed.add(label)
        return {
            "type": "image.download",
            "image": label,
            "receivedBytes": received,
            **({"totalBytes": total} if total is not None else {}),
        }


def create_app(*, auth_token: str | None = None) -> FastAPI:
    """Build an app with an isolated, process-local sandbox inventory.

    ``auth_token`` is required for SDK sessions. Omitting it keeps the
    manually started development server backward compatible.
    """
    sandboxes: dict[str, SmolVM] = {}
    browser_sessions: dict[str, DisplaySandboxProtocol] = {}
    computer_sessions: dict[str, ComputerSandboxProtocol] = {}
    computer_creating: set[str] = set()
    computer_lock = threading.Lock()
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
        for computer in list(computer_sessions.values()):
            with suppress(Exception):
                await asyncio.to_thread(computer.delete)
            with suppress(Exception):
                computer.close()
        computer_sessions.clear()
        for browser in list(browser_sessions.values()):
            with suppress(Exception):
                await asyncio.to_thread(browser.delete)
            with suppress(Exception):
                browser.close()
        browser_sessions.clear()
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
    app.state.browser_sessions = browser_sessions
    app.state.computer_sessions = computer_sessions
    app.state.computer_creating = computer_creating

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

    def browser_response(browser: DisplaySandboxProtocol) -> BrowserSessionResponse:
        if browser.cdp_url is None:
            raise _sdk_error(
                409,
                "browser_endpoint_unavailable",
                f"Browser session '{browser.session_id}' has no automation endpoint; "
                "delete it and create the session again.",
            )
        return BrowserSessionResponse(
            session_id=browser.session_id,
            sandbox_id=browser.vm_id,
            status=browser.status,
            cdp_url=browser.cdp_url,
            viewer_url=browser.viewer_url,
            display_url=browser.display_url,
            profile_id=browser.info.profile_id,
        )

    def resolve_browser(session_id: str) -> DisplaySandboxProtocol:
        browser = browser_sessions.get(session_id)
        if browser is None:
            raise _sdk_error(
                404,
                "browser_deleted",
                f"Browser session '{session_id}' is unavailable; create a new browser session.",
            )
        return browser

    def computer_response(computer: ComputerSandboxProtocol) -> ComputerResponse:
        return ComputerResponse(
            computer_id=computer.computer_id,
            sandbox_id=computer.sandbox_id,
            template="linux-desktop",
            status="ready",
            capabilities=computer.capabilities,
            display=ComputerDisplayResponse(
                viewer_url=computer.display.viewer_url,
                vnc_url=computer.display.vnc_url,
            ),
            browser=ComputerBrowserResponse(
                status=computer.browser.status,
                cdp_url=computer.browser.cdp_url,
            ),
        )

    def resolve_computer(computer_id: str) -> ComputerSandboxProtocol:
        with computer_lock:
            computer = computer_sessions.get(computer_id)
        if computer is None:
            raise _sdk_error(
                404,
                "computer_deleted",
                f"Computer '{computer_id}' is unavailable; create a new computer.",
            )
        return computer

    async def write_vm_file(
        vm: SmolVM,
        *,
        resource_kind: str,
        resource_id: str,
        path: str,
        request: Request,
    ) -> Response:
        if not PurePosixPath(path).is_absolute():
            raise _sdk_error(
                400,
                "invalid_path",
                f"File path for {resource_kind} '{resource_id}' must be absolute; "
                "retry files.write('/workspace/file', content).",
            )
        try:
            declared_size = int(request.headers.get("content-length", "0"))
        except ValueError as exc:
            raise _sdk_error(
                400,
                "transport_failed",
                f"File size for {resource_kind} '{resource_id}' must be an integer; "
                "retry the same files.write() call.",
            ) from exc
        if declared_size > _MAX_FILE_BYTES:
            raise _sdk_error(
                413,
                "transport_failed",
                f"File for {resource_kind} '{resource_id}' exceeds the 16 MiB SDK limit; "
                "retry files.write() with a smaller file.",
            )
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="smolvm-sdk-upload-", delete=False) as handle:
                temporary = Path(handle.name)
                received = 0
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > _MAX_FILE_BYTES:
                        raise _sdk_error(
                            413,
                            "transport_failed",
                            f"File for {resource_kind} '{resource_id}' exceeds the 16 MiB SDK "
                            "limit; retry files.write() with a smaller file.",
                        )
                    handle.write(chunk)
            await asyncio.to_thread(vm.upload_file, temporary, path)
        except HTTPException:
            raise
        except (ValueError, SmolVMError) as exc:
            raise _sdk_error(
                409,
                "transport_failed",
                f"Could not write '{path}' in {resource_kind} '{resource_id}'; "
                "check the path and retry the same files.write() call.",
            ) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return Response(status_code=204)

    async def read_vm_file(
        vm: SmolVM,
        *,
        resource_kind: str,
        resource_id: str,
        path: str,
    ) -> Response:
        if not PurePosixPath(path).is_absolute():
            raise _sdk_error(
                400,
                "invalid_path",
                f"File path for {resource_kind} '{resource_id}' must be absolute; "
                "retry files.read('/workspace/file').",
            )
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
                raise _sdk_error(
                    413,
                    "transport_failed",
                    f"File in {resource_kind} '{resource_id}' exceeds the 16 MiB SDK limit; "
                    "choose a smaller file and retry files.read().",
                )
            with tempfile.NamedTemporaryFile(prefix="smolvm-sdk-download-", delete=False) as handle:
                temporary = Path(handle.name)
            await asyncio.to_thread(
                vm.download_file,
                path,
                temporary,
                max_bytes=_MAX_FILE_BYTES,
            )
            if temporary.stat().st_size > _MAX_FILE_BYTES:
                raise _sdk_error(
                    413,
                    "transport_failed",
                    f"File in {resource_kind} '{resource_id}' exceeds the 16 MiB SDK limit; "
                    "choose a smaller file and retry files.read().",
                )
            content = temporary.read_bytes()
        except HTTPException:
            raise
        except (ValueError, SmolVMError) as exc:
            raise _sdk_error(
                409,
                "transport_failed",
                f"Could not read '{path}' from {resource_kind} '{resource_id}'; "
                "check the path and retry the same files.read() call.",
            ) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return Response(content=content, media_type="application/octet-stream")

    @app.post(
        "/computers",
        response_model=ComputerResponse,
        status_code=201,
        operation_id="createComputer",
        responses={400: {"model": ErrorResponse}},
    )
    def create_computer(body: CreateComputerRequest) -> ComputerResponse:
        computer_id = body.computer_id or f"computer-{secrets.token_hex(4)}"
        with computer_lock:
            if computer_id in computer_sessions or computer_id in computer_creating:
                raise _sdk_error(
                    409,
                    "computer_already_exists",
                    f"Computer '{computer_id}' already exists; delete it or choose another name.",
                )
            computer_creating.add(computer_id)
        publish({"type": "computer.starting", "computerId": computer_id})
        computer: ComputerSandboxProtocol | None = None
        network = body.network.model_dump()
        internet_settings = None if network["mode"] == "open" else network
        try:
            computer = SmolVM.computer(
                template=body.template,
                name=computer_id,
                backend=body.backend,
                display=body.display.model_dump(),
                resources=body.resources.model_dump(),
                network=internet_settings,
                workspace=body.workspace,
            )
            result = computer_response(computer)
            with computer_lock:
                computer_sessions[computer_id] = computer
        except HTTPException:
            if computer is not None:
                with suppress(Exception):
                    computer.delete()
                with suppress(Exception):
                    computer.close()
            raise
        except (ValueError, SmolVMError) as exc:
            if computer is not None:
                with suppress(Exception):
                    computer.delete()
                with suppress(Exception):
                    computer.close()
            code = (
                "computer_image_unavailable"
                if isinstance(exc, ImageError)
                else "computer_create_failed"
            )
            raise _sdk_error(
                400,
                code,
                f"Could not create computer '{computer_id}': {exc}. Fix the options and try again.",
            ) from exc
        finally:
            with computer_lock:
                computer_creating.discard(computer_id)
        publish(
            {
                "type": "computer.ready",
                "computerId": computer_id,
                "sandboxId": result.sandbox_id,
            }
        )
        return result

    @app.delete("/computers/{computer_id}", status_code=204, operation_id="deleteComputer")
    def delete_computer(computer_id: str) -> Response:
        with computer_lock:
            computer = computer_sessions.get(computer_id)
            creating = computer_id in computer_creating
        if computer is None:
            if creating:
                raise _sdk_error(
                    409,
                    "computer_not_ready",
                    f"Computer '{computer_id}' is still starting; wait for creation to finish, "
                    "then call computer.delete() again.",
                )
            return Response(status_code=204)
        publish(
            {
                "type": "computer.stopping",
                "computerId": computer_id,
                "sandboxId": computer.sandbox_id,
            }
        )
        try:
            computer.delete()
        except (ValueError, SmolVMError) as exc:
            raise _sdk_error(
                409,
                "cleanup_failed",
                f"Computer '{computer_id}' could not be deleted; call computer.delete() again.",
            ) from exc
        with suppress(Exception):
            computer.close()
        with computer_lock:
            if computer_sessions.get(computer_id) is computer:
                computer_sessions.pop(computer_id, None)
        publish(
            {
                "type": "computer.deleted",
                "computerId": computer_id,
                "sandboxId": computer.sandbox_id,
            }
        )
        return Response(status_code=204)

    @app.post("/computers/{computer_id}/cancel", status_code=204)
    def cancel_computer_operation(computer_id: str) -> Response:
        """Stop an in-flight operation by deleting its computer VM."""
        return delete_computer(computer_id)

    @app.post(
        "/computers/{computer_id}/exec",
        response_model=ExecResponse,
        operation_id="execComputerCommand",
    )
    def exec_computer_command(computer_id: str, body: ExecRequest) -> ExecResponse:
        computer = resolve_computer(computer_id)
        try:
            command, shell = _command_with_context(body)
            started = time.monotonic()
            result = computer.run(command, body.timeout, shell)
            duration_ms = round((time.monotonic() - started) * 1000)
        except OperationTimeoutError as exc:
            deleted = False
            try:
                computer.delete()
                deleted = True
            except Exception:
                logger.exception("Could not delete timed-out SDK computer %s", computer_id)
            if deleted:
                with suppress(Exception):
                    computer.close()
                with computer_lock:
                    if computer_sessions.get(computer_id) is computer:
                        computer_sessions.pop(computer_id, None)
            raise _sdk_error(
                408,
                "command_timeout",
                (
                    f"Command timed out in computer '{computer_id}'; "
                    + (
                        "the computer was deleted to confirm the command stopped. "
                        "Create a new computer and retry."
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
                f"Command could not run in computer '{computer_id}'; delete it and create another.",
            ) from exc
        return ExecResponse(**result.model_dump(), duration_ms=duration_ms)

    @app.put(
        "/computers/{computer_id}/files",
        status_code=204,
        operation_id="writeComputerFile",
    )
    async def write_computer_file(computer_id: str, path: str, request: Request) -> Response:
        computer = resolve_computer(computer_id)
        if not PurePosixPath(path).is_absolute():
            raise _sdk_error(
                400,
                "invalid_path",
                f"File path for computer '{computer_id}' must be absolute; "
                "retry files.write('/workspace/file', content).",
            )
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > _MAX_FILE_BYTES:
                raise _sdk_error(
                    413,
                    "transport_failed",
                    f"File for computer '{computer_id}' exceeds the 16 MiB SDK limit; "
                    "retry with a smaller file.",
                )
        try:
            await asyncio.to_thread(computer.files.write, path, bytes(content))
        except (ValueError, SmolVMError) as exc:
            raise _sdk_error(
                409,
                "transport_failed",
                f"Could not write '{path}' in computer '{computer_id}'; check the path and retry.",
            ) from exc
        return Response(status_code=204)

    @app.get(
        "/computers/{computer_id}/files",
        response_class=Response,
        operation_id="readComputerFile",
    )
    async def read_computer_file(computer_id: str, path: str) -> Response:
        computer = resolve_computer(computer_id)
        try:
            size_result = await asyncio.to_thread(
                computer.run,
                f"stat -c %s -- {shlex.quote(path)}",
                30,
                "raw",
            )
            if not size_result.ok:
                raise SmolVMError(size_result.stderr.strip() or f"Could not inspect '{path}'.")
            size = int(size_result.stdout.strip())
            if size > _MAX_FILE_BYTES:
                raise _sdk_error(
                    413,
                    "transport_failed",
                    f"File in computer '{computer_id}' exceeds the 16 MiB SDK limit; "
                    "choose a smaller file.",
                )
            content = await asyncio.to_thread(
                computer.files.read,
                path,
                max_bytes=_MAX_FILE_BYTES,
            )
        except HTTPException:
            raise
        except (ValueError, SmolVMError) as exc:
            raise _sdk_error(
                409,
                "transport_failed",
                f"Could not read '{path}' from computer '{computer_id}'; check the path and retry.",
            ) from exc
        if len(content) > _MAX_FILE_BYTES:
            raise _sdk_error(
                413,
                "transport_failed",
                f"File in computer '{computer_id}' exceeds the 16 MiB SDK limit; "
                "choose a smaller file.",
            )
        return Response(content=content, media_type="application/octet-stream")

    @app.post(
        "/computers/{computer_id}/browser/launch",
        response_model=ComputerBrowserResponse,
        operation_id="launchComputerBrowser",
    )
    def launch_computer_browser(computer_id: str) -> ComputerBrowserResponse:
        computer = resolve_computer(computer_id)
        try:
            computer.browser.launch()
        except (ValueError, SmolVMError) as exc:
            raise _sdk_error(
                409,
                "browser_launch_failed",
                f"Chromium did not open in computer '{computer_id}'; "
                "inspect its logs and try again.",
            ) from exc
        return ComputerBrowserResponse(
            status=computer.browser.status,
            cdp_url=computer.browser.cdp_url,
        )

    @app.post(
        "/browser-sessions",
        response_model=BrowserSessionResponse,
        status_code=201,
        operation_id="createBrowserSession",
        responses={400: {"model": ErrorResponse}},
    )
    def create_browser_session(body: CreateBrowserSessionRequest) -> BrowserSessionResponse:
        session_id = body.session_id or f"browser-{secrets.token_hex(4)}"
        publish({"type": "browser.starting", "sessionId": session_id})
        browser: DisplaySandboxProtocol | None = None
        network = body.network.model_dump()
        internet_settings = None if network["mode"] == "open" else network
        try:
            browser = SmolVM.browser(
                headless=body.mode == "headless",
                session_id=session_id,
                backend=body.backend,
                profile_id=body.profile_id,
                persistent=body.profile_mode == "persistent",
                timeout_minutes=body.timeout_minutes,
                viewport=body.viewport.model_dump(),
                record_video=body.record_video,
                allow_downloads=body.allow_downloads,
                internet_settings=internet_settings,
                memory_mb=body.memory,
                disk_size_mb=body.disk_size,
            )
            result = browser_response(browser)
            browser_sessions[session_id] = browser
        except HTTPException:
            browser_sessions.pop(session_id, None)
            if browser is not None:
                with suppress(Exception):
                    browser.delete()
                with suppress(Exception):
                    browser.close()
            raise
        except (ValueError, SmolVMError) as exc:
            if browser is not None:
                with suppress(Exception):
                    browser.delete()
                with suppress(Exception):
                    browser.close()
            code = (
                "browser_image_unavailable"
                if isinstance(exc, ImageError)
                else "browser_create_failed"
            )
            raise _sdk_error(
                400,
                code,
                f"Could not create browser session '{session_id}': {exc}. "
                "Fix the options and try again.",
            ) from exc
        publish(
            {
                "type": "browser.ready",
                "sessionId": result.session_id,
                "sandboxId": result.sandbox_id,
            }
        )
        return result

    @app.delete(
        "/browser-sessions/{session_id}",
        status_code=204,
        operation_id="deleteBrowserSession",
    )
    def delete_browser_session(session_id: str) -> Response:
        browser = browser_sessions.get(session_id)
        if browser is None:
            return Response(status_code=204)
        publish(
            {
                "type": "browser.stopping",
                "sessionId": session_id,
                "sandboxId": browser.vm_id,
            }
        )
        try:
            browser.delete()
        except (ValueError, SmolVMError) as exc:
            raise _sdk_error(
                409,
                "cleanup_failed",
                f"Browser session '{session_id}' could not be deleted; call "
                "browser.delete() again, or close the SmolVM client.",
            ) from exc
        finally:
            with suppress(Exception):
                browser.close()
        browser_sessions.pop(session_id, None)
        publish(
            {
                "type": "browser.deleted",
                "sessionId": session_id,
                "sandboxId": browser.vm_id,
            }
        )
        return Response(status_code=204)

    @app.post(
        "/browser-sessions/{session_id}/exec",
        response_model=ExecResponse,
        operation_id="execBrowserCommand",
    )
    def exec_browser_command(session_id: str, body: ExecRequest) -> ExecResponse:
        browser = resolve_browser(session_id)
        try:
            command, _shell = _command_with_context(body)
            guest_command = f"runuser -u agent -- sh -c {shlex.quote(command)}"
            started = time.monotonic()
            result = browser.vm.run(guest_command, body.timeout, "raw")
            duration_ms = round((time.monotonic() - started) * 1000)
        except OperationTimeoutError as exc:
            deleted = False
            try:
                browser.delete()
                deleted = True
            except Exception:
                logger.exception("Could not delete timed-out browser session %s", session_id)
            finally:
                with suppress(Exception):
                    browser.close()
            if deleted:
                browser_sessions.pop(session_id, None)
            raise _sdk_error(
                408,
                "command_timeout",
                (
                    f"Browser command timed out in session '{session_id}'; "
                    + (
                        "the session was deleted to confirm the command stopped. "
                        "Create a new browser session and retry."
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
                f"Command could not run in browser session '{session_id}'; create a new "
                "browser session if it is no longer usable.",
            ) from exc
        return ExecResponse(**result.model_dump(), duration_ms=duration_ms)

    @app.put(
        "/browser-sessions/{session_id}/files",
        status_code=204,
        operation_id="writeBrowserFile",
        responses={404: {"model": ErrorResponse}},
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                },
            }
        },
    )
    async def write_browser_file(session_id: str, path: str, request: Request) -> Response:
        browser = resolve_browser(session_id)
        return await write_vm_file(
            browser.vm,
            resource_kind="browser session",
            resource_id=session_id,
            path=path,
            request=request,
        )

    @app.get(
        "/browser-sessions/{session_id}/files",
        response_class=Response,
        responses={
            200: {
                "content": {
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                }
            },
            404: {"model": ErrorResponse},
        },
        operation_id="readBrowserFile",
    )
    async def read_browser_file(session_id: str, path: str) -> Response:
        browser = resolve_browser(session_id)
        return await read_vm_file(
            browser.vm,
            resource_kind="browser session",
            resource_id=session_id,
            path=path,
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
        download_events = _DownloadProgressEvents()

        def on_download(label: str, chunk: int, total: int | None) -> None:
            event = download_events.update(label, chunk, total)
            if event is not None:
                publish(event)

        try:
            sandbox = SmolVM(**values, on_download=on_download)
            sandboxes[sandbox.vm_id] = sandbox
            sandbox.start()
        except (ValueError, SmolVMError) as exc:
            if sandbox is not None:
                with suppress(Exception):
                    sandbox.delete()
                with suppress(Exception):
                    sandbox.close()
                sandboxes.pop(sandbox.vm_id, None)
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
        return await write_vm_file(
            vm,
            resource_kind="sandbox",
            resource_id=sandbox_id,
            path=path,
            request=request,
        )

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
        return await read_vm_file(
            vm,
            resource_kind="sandbox",
            resource_id=sandbox_id,
            path=path,
        )

    return app
