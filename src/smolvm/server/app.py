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

This PoC exposes two endpoints:

- ``POST /sandboxes``  — create, boot, and register a sandbox.
- ``GET  /sandboxes/{id}`` — fetch a registered sandbox's state.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException

from smolvm.exceptions import SmolVMError, VMNotFoundError
from smolvm.facade import SmolVM
from smolvm.server.models import (
    CreateSandboxRequest,
    ErrorResponse,
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

    # Live facade instances, keyed by sandbox id. Lives for the process
    # lifetime; a future revision will add eviction / cleanup on stop.
    sandboxes: dict[str, SmolVM] = {}

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

        The in-memory registry is treated as a cache, not the source of
        truth: on a miss we try to reconnect to a sandbox that exists on
        the host (e.g. one created before this server process started)
        via :meth:`SmolVM.from_id`, and backfill the registry so later
        calls hit the fast path. Only a sandbox that exists nowhere on
        the host yields a 404.
        """
        vm = sandboxes.get(sandbox_id)
        if vm is None:
            try:
                vm = SmolVM.from_id(sandbox_id)
                # Backfill the cache so the next GET hits the fast path.
                # from_id binds vm_id verbatim, so sandbox_id == vm.vm_id here.
                sandboxes[sandbox_id] = vm
            except VMNotFoundError:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"No sandbox named '{sandbox_id}'. Create one with "
                        f"POST /sandboxes, or list active sandboxes."
                    ),
                ) from None
            except (ValueError, SmolVMError) as exc:
                # The sandbox exists but could not be reconnected (e.g. it
                # is in a bad state or its control channel is unreachable).
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Could not reconnect to sandbox '{sandbox_id}': {exc}. "
                        f"Create a fresh one with POST /sandboxes."
                    ),
                ) from exc
        vm.refresh()
        return SandboxResponse(id=vm.vm_id, status=vm.status)

    return app
