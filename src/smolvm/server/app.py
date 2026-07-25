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

"""FastAPI application wrapping the SmolVM facade.

The app keeps a process-level registry of live :class:`~smolvm.SmolVM`
objects keyed by sandbox id. A later ``GET`` or ``exec`` finds the same
object created by an earlier ``POST``. The API does not read the CLI's
persistent sandbox inventory.

It exposes the sandbox lifecycle:

- ``POST   /sandboxes``           — create, boot, and register a sandbox.
- ``GET    /sandboxes``           — list every sandbox on the host.
- ``GET    /sandboxes/{id}``      — fetch a sandbox's state.
- ``DELETE /sandboxes/{id}``      — stop the sandbox and forget it.
- ``GET    /sandboxes/{id}/desktop`` — get a local desktop endpoint.
- ``POST   /sandboxes/{id}/exec`` — run a command inside the sandbox.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, Response

from smolvm.exceptions import SmolVMError
from smolvm.facade import SmolVM
from smolvm.server.models import (
    CreateSandboxRequest,
    DesktopResponse,
    ErrorResponse,
    ExecRequest,
    ExecResponse,
    SandboxResponse,
)

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Build and return the SmolVM HTTP application.

    A factory (rather than a module-level ``app``) keeps the registry
    scoped per-app, which makes the server testable: each test can spin
    up a fresh app with an empty registry.
    """
    # Live facade instances are the API's complete process-local inventory.
    # The API deliberately does not read the CLI's SQLite registry.
    sandboxes: dict[str, SmolVM] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        yield
        # API-owned sandboxes are process-scoped. Graceful server shutdown
        # tears them down instead of leaving undiscoverable child processes.
        for sandbox in list(sandboxes.values()):
            with suppress(Exception):
                await asyncio.to_thread(sandbox.delete)
            with suppress(Exception):
                sandbox.close()
        sandboxes.clear()

    app = FastAPI(
        title="SmolVM",
        summary="Disposable computers for AI agents, over HTTP.",
        version="0.1.0",
        lifespan=lifespan,
    )

    def _resolve(sandbox_id: str) -> SmolVM:
        """Return the live facade for ``sandbox_id``, reconnecting on miss.

        The registry is the API's source of truth. A sandbox created by
        another process is intentionally invisible here.

        Raises:
            HTTPException: 404 if this API process does not own the ID.
        """
        vm = sandboxes.get(sandbox_id)
        if vm is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Sandbox '{sandbox_id}' was not found; run GET /sandboxes "
                    f"to list ids or POST /sandboxes to create one."
                ),
            )
        return vm

    @app.post(
        "/sandboxes",
        response_model=SandboxResponse,
        status_code=201,
        operation_id="createSandbox",
        responses={
            400: {
                "model": ErrorResponse,
                "description": "The request was invalid or the sandbox failed to boot.",
            },
        },
    )
    def create_sandbox(body: CreateSandboxRequest) -> SandboxResponse:
        """Create, boot, and register a new sandbox.

        Builds a :class:`~smolvm.SmolVM` from the request's auto-config
        fields, starts it, stores it in the registry under its id, and
        returns the client-safe view.
        """
        try:
            sandbox = SmolVM(**body.model_dump(exclude_none=True))
            sandbox.start()
        except (ValueError, SmolVMError) as exc:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Could not create the sandbox: {exc}. Fix the request and "
                    f"POST it to /sandboxes again."
                ),
            ) from exc

        sandboxes[sandbox.vm_id] = sandbox
        return SandboxResponse(id=sandbox.vm_id, status=sandbox.status)

    @app.get(
        "/sandboxes/{sandbox_id}",
        response_model=SandboxResponse,
        operation_id="getSandbox",
        responses={
            404: {
                "model": ErrorResponse,
                "description": "No sandbox with that id exists on the host.",
            },
            409: {
                "model": ErrorResponse,
                "description": "The sandbox exists but could not be reconnected.",
            },
        },
    )
    def get_sandbox(sandbox_id: str) -> SandboxResponse:
        """Return the current state of a sandbox.

        A sandbox not owned by this API process yields a 404.
        """
        vm = _resolve(sandbox_id)
        vm.refresh()
        return SandboxResponse(id=vm.vm_id, status=vm.status)

    @app.get(
        "/sandboxes",
        response_model=list[SandboxResponse],
        operation_id="listSandboxes",
    )
    def list_sandboxes() -> list[SandboxResponse]:
        """List the sandboxes discoverable on the host.

        Returns only sandboxes owned by this API process.
        """
        responses: list[SandboxResponse] = []
        for vm_id, vm in sorted(sandboxes.items()):
            vm.refresh()
            responses.append(SandboxResponse(id=vm_id, status=vm.status))
        return responses

    @app.get(
        "/sandboxes/{sandbox_id}/desktop",
        response_model=DesktopResponse,
        operation_id="getSandboxDesktop",
        responses={
            404: {"model": ErrorResponse, "description": "The sandbox was not found."},
            409: {"model": ErrorResponse, "description": "No running desktop is available."},
        },
    )
    def get_sandbox_desktop(sandbox_id: str) -> DesktopResponse:
        """Return a sanitized loopback desktop endpoint without opening it."""
        vm = _resolve(sandbox_id)
        vm.refresh()
        endpoint = vm.desktop_endpoint
        if endpoint is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Sandbox '{sandbox_id}' has no running desktop; start it, then GET "
                    f"/sandboxes/{sandbox_id}/desktop again."
                ),
            )
        return DesktopResponse(
            protocol=endpoint.protocol,
            host=endpoint.host,
            port=endpoint.port,
            viewer_url=endpoint.viewer_url,
        )

    @app.delete(
        "/sandboxes/{sandbox_id}",
        status_code=204,
        operation_id="deleteSandbox",
        responses={
            404: {
                "model": ErrorResponse,
                "description": "No sandbox with that id exists on the host.",
            },
            409: {
                "model": ErrorResponse,
                "description": "The sandbox could not be reconnected or deleted.",
            },
        },
    )
    def delete_sandbox(sandbox_id: str) -> Response:
        """Stop the sandbox, release its resources, and forget it.

        Evicts the facade from the registry so its id stops resolving —
        the write-through delete the registry-as-cache model needs.
        """
        vm = _resolve(sandbox_id)
        try:
            vm.delete()
        except (ValueError, SmolVMError) as exc:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Sandbox '{sandbox_id}' could not be deleted; retry "
                    f"DELETE /sandboxes/{sandbox_id}."
                ),
            ) from exc
        sandboxes.pop(vm.vm_id, None)
        return Response(status_code=204)

    @app.post(
        "/sandboxes/{sandbox_id}/exec",
        response_model=ExecResponse,
        operation_id="execCommand",
        responses={
            404: {
                "model": ErrorResponse,
                "description": "No sandbox with that id exists on the host.",
            },
            409: {
                "model": ErrorResponse,
                "description": (
                    "The sandbox could not be reconnected, or the command could not run."
                ),
            },
        },
    )
    def exec_command(sandbox_id: str, body: ExecRequest) -> ExecResponse:
        """Run a command inside a sandbox and return its result.

        Resolves the sandbox (reconnecting on a registry miss), then runs
        the command over the facade's cached SSH channel.
        """

        vm = _resolve(sandbox_id)
        try:
            result = vm.run(body.command, body.timeout, body.shell)
        except (ValueError, SmolVMError) as exc:
            # The sandbox exists but the command could not run (e.g. it is
            # not running, or has no SSH-capable channel). A command that
            # runs and exits non-zero is NOT an error — that is a
            # successful exec returning a non-zero exit_code below.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Command could not run in sandbox '{sandbox_id}'; check it "
                    f"is running with GET /sandboxes/{sandbox_id}."
                ),
            ) from exc
        return ExecResponse(**result.model_dump())

    return app
