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
facade instances keyed by sandbox id. HTTP is stateless but the facade
is stateful (it owns the SSH/control channel to the guest), so the
registry is what lets a later ``GET`` or ``exec`` find the sandbox a
prior ``POST`` created. This is the standard local-daemon pattern.

It exposes the sandbox lifecycle:

- ``POST   /sandboxes``           — create, boot, and register a sandbox.
- ``GET    /sandboxes``           — list every sandbox on the host.
- ``GET    /sandboxes/{id}``      — fetch a sandbox's state.
- ``DELETE /sandboxes/{id}``      — stop the sandbox and forget it.
- ``GET    /sandboxes/{id}/desktop`` — get a local desktop endpoint.
- ``POST   /sandboxes/{id}/exec`` — run a command inside the sandbox.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Response

from smolvm.exceptions import SmolVMError, VMNotFoundError
from smolvm.facade import SmolVM, _existing_vm_ids
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
    app = FastAPI(
        title="SmolVM",
        summary="Disposable computers for AI agents, over HTTP.",
        version="0.1.0",
    )

    # Live facade instances, keyed by sandbox id. Acts as a write-through
    # cache over the host: misses reconnect via SmolVM.from_id and backfill;
    # DELETE evicts. A facade owns the SSH/control channel, so caching it
    # also avoids re-handshaking on every exec.
    sandboxes: dict[str, SmolVM] = {}

    def _resolve(sandbox_id: str) -> SmolVM:
        """Return the live facade for ``sandbox_id``, reconnecting on miss.

        The registry is a cache, not the source of truth: on a miss we
        reconnect to a sandbox that exists on the host (e.g. one created
        before this server process started) via :meth:`SmolVM.from_id`
        and backfill the registry so later calls hit the fast path.

        Raises:
            HTTPException: 404 if no such sandbox exists anywhere on the
                host; 409 if it exists but cannot be reconnected.
        """
        vm = sandboxes.get(sandbox_id)
        if vm is not None:
            return vm
        try:
            vm = SmolVM.from_id(sandbox_id)
            # from_id binds vm_id verbatim, so sandbox_id == vm.vm_id here.
            sandboxes[sandbox_id] = vm
        except VMNotFoundError:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Sandbox '{sandbox_id}' was not found; run GET /sandboxes "
                    f"to list ids or POST /sandboxes to create one."
                ),
            ) from None
        except (ValueError, SmolVMError) as exc:
            # The sandbox exists but could not be reconnected (e.g. it is
            # in a bad state or its control channel is unreachable).
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Sandbox '{sandbox_id}' could not be reconnected; run "
                    f"DELETE /sandboxes/{sandbox_id} then POST /sandboxes."
                ),
            ) from exc
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

        On a registry miss the sandbox is reconnected from the host, so
        only a sandbox that exists nowhere yields a 404.
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

        Enumerates host VM ids directly rather than the in-memory
        registry, so sandboxes created before this server started are
        included. Sandboxes that cannot be reconnected are omitted.
        """
        responses: list[SandboxResponse] = []
        for vm_id in sorted(_existing_vm_ids()):
            try:
                vm = _resolve(vm_id)
                vm.refresh()
            except HTTPException:
                # A sandbox that vanished or cannot be reconnected between
                # listing and resolving is skipped rather than failing the
                # whole list.
                continue
            responses.append(SandboxResponse(id=vm.vm_id, status=vm.status))
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
