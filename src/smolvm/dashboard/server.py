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

"""Nebula Dashboard - FastAPI bridge server.

Connects the SmolVM Python SDK to the React+Vite frontend via REST
endpoints and WebSocket streaming.

Usage:
    uvicorn smolvm.dashboard.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from smolvm.dashboard.commands import CommandAction, parse_command
from smolvm.dashboard.connection_manager import ConnectionManager
from smolvm.dashboard.poller import poll_vm_state
from smolvm.exceptions import VMNotFoundError
from smolvm.storage import StateManager
from smolvm.types import VMInfo, VMState
from smolvm.vm import SmolVMManager, resolve_data_dir

logger = logging.getLogger(__name__)


# --- Accessor helpers for app.state ---
def _get_sdk(app: FastAPI) -> SmolVMManager:
    """Get the SDK instance from app.state, raising if not initialized."""
    sdk: SmolVMManager | None = getattr(app.state, "sdk", None)
    if sdk is None:
        raise RuntimeError("SmolVMManager not initialized.")
    return sdk


def _get_state_manager(app: FastAPI) -> StateManager:
    """Get the StateManager instance from app.state, raising if not initialized."""
    sm: StateManager | None = getattr(app.state, "state_manager", None)
    if sm is None:
        raise RuntimeError("StateManager not initialized.")
    return sm


def _get_conn_manager(app: FastAPI) -> ConnectionManager:
    """Get the ConnectionManager from app.state."""
    cm: ConnectionManager | None = getattr(app.state, "conn_manager", None)
    if cm is None:
        raise RuntimeError("ConnectionManager not initialized.")
    return cm


# --- Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    """Application lifespan: initialize SDK and start background poller."""
    data_dir = resolve_data_dir()
    db_path = data_dir / "smolvm.db"

    app.state.state_manager = StateManager(db_path)
    app.state.sdk = SmolVMManager(data_dir=data_dir)
    app.state.conn_manager = ConnectionManager()

    # Reconcile stale VMs on startup
    stale = await asyncio.to_thread(app.state.state_manager.reconcile)
    if stale:
        logger.warning("Reconciled %d stale VMs on startup.", len(stale))

    # Start background poller
    poller_task = asyncio.create_task(
        poll_vm_state(app.state.state_manager, app.state.conn_manager)
    )

    logger.info("Nebula Dashboard started. Data dir: %s", data_dir)
    yield

    # Shutdown
    poller_task.cancel()
    with suppress(asyncio.CancelledError):
        _ = await poller_task  # Ensure poller task finishes cleanup.

    sdk: SmolVMManager | None = getattr(app.state, "sdk", None)
    if sdk is not None:
        sdk.close()

    logger.info("Nebula Dashboard stopped.")


# --- App ---
app = FastAPI(
    title="SmolVM Nebula Dashboard",
    description="Control plane for AI microVMs",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS: Allow dev server during development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # Vite dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Pydantic models for API ---
class CommandRequest(BaseModel):
    """Request body for the command bar."""

    text: str


class CommandResponse(BaseModel):
    """Response from a command execution.

    Not yet used as the return type but documents the API contract.
    """

    action: str
    target: str
    result: str
    affected_vms: list[str]


class VMSummary(BaseModel):
    """Lightweight VM representation for the particle system."""

    vm_id: str
    status: str


# --- Serialization helper ---
def _vm_info_to_dict(vm: VMInfo) -> dict[str, Any]:
    """Convert VMInfo to a JSON-serializable dict."""
    return {
        "vm_id": vm.vm_id,
        "status": vm.status.value,
        "config": {
            "vcpu_count": vm.config.vcpu_count,
            "mem_size_mib": vm.config.mem_size_mib,
        },
        "network": (
            {
                "guest_ip": vm.network.guest_ip,
                "gateway_ip": vm.network.gateway_ip,
                "tap_device": vm.network.tap_device,
                "ssh_host_port": vm.network.ssh_host_port,
            }
            if vm.network
            else None
        ),
        "pid": vm.pid,
    }


# =====================================================================
# REST Endpoints
# =====================================================================


@app.get("/api/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok", "service": "nebula-dashboard"}


@app.get("/api/vms")
async def list_vms(status: str | None = None) -> list[dict[str, Any]]:
    """List all VMs, optionally filtered by status.

    Args:
        status: Filter by VM status (created/running/stopped/error).
    """
    sm = _get_state_manager(app)
    filter_state: VMState | None = None
    if status:
        try:
            filter_state = VMState(status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}") from None

    vms = await asyncio.to_thread(sm.list_vms, filter_state)

    return [_vm_info_to_dict(vm) for vm in vms]


@app.get("/api/vms/particles")
async def list_particles() -> list[VMSummary]:
    """Lightweight endpoint for the particle system.

    Returns only vm_id and status — no heavy JSON deserialization.
    """
    sm = _get_state_manager(app)
    vms = await asyncio.to_thread(sm.list_vms)
    return [VMSummary(vm_id=vm.vm_id, status=vm.status.value) for vm in vms]


@app.get("/api/vms/{vm_id}")
async def get_vm(vm_id: str) -> dict[str, Any]:
    """Get detailed information about a specific VM."""
    sm = _get_state_manager(app)
    try:
        vm = await asyncio.to_thread(sm.get_vm, vm_id)
    except VMNotFoundError:
        raise HTTPException(status_code=404, detail=f"VM not found: {vm_id}") from None

    return _vm_info_to_dict(vm)


@app.delete("/api/vms/{vm_id}")
async def delete_vm(vm_id: str) -> dict[str, str]:
    """Delete a VM and release all resources."""
    sdk = _get_sdk(app)
    try:
        await asyncio.to_thread(sdk.delete, vm_id)
    except VMNotFoundError:
        raise HTTPException(status_code=404, detail=f"VM not found: {vm_id}") from None

    return {"status": "deleted", "vm_id": vm_id}


@app.post("/api/vms/{vm_id}/stop")
async def stop_vm(vm_id: str) -> dict[str, Any]:
    """Stop a running VM."""
    sdk = _get_sdk(app)
    try:
        info = await asyncio.to_thread(sdk.stop, vm_id)
    except VMNotFoundError:
        raise HTTPException(status_code=404, detail=f"VM not found: {vm_id}") from None

    return _vm_info_to_dict(info)


@app.post("/api/command", response_model=CommandResponse)
async def execute_command(request: CommandRequest) -> CommandResponse | JSONResponse:
    """Execute a command-style action from the command bar."""
    sdk = _get_sdk(app)
    sm = _get_state_manager(app)
    parsed = parse_command(request.text)

    affected: list[str] = []
    result_msg = ""

    if parsed.action == CommandAction.LIST:
        filter_state = None
        if parsed.target:
            try:
                filter_state = VMState(parsed.target)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"error": f"Unknown status: {parsed.target}"},
                )
        vms = await asyncio.to_thread(sm.list_vms, filter_state)
        affected = [vm.vm_id for vm in vms]
        result_msg = f"Found {len(affected)} VMs."

    elif parsed.action == CommandAction.DELETE:
        vms = await asyncio.to_thread(sm.list_vms)
        targets = _resolve_targets(vms, parsed.target)
        for vm_id in targets:
            try:
                await asyncio.to_thread(sdk.delete, vm_id)
                affected.append(vm_id)
            except VMNotFoundError:
                logger.warning("VM %s already deleted, skipping.", vm_id)
            except Exception:
                logger.warning("Failed to delete VM %s", vm_id, exc_info=True)
        result_msg = f"Deleted {len(affected)} VMs."

    elif parsed.action == CommandAction.STOP:
        vms = await asyncio.to_thread(sm.list_vms)
        targets = _resolve_targets(vms, parsed.target)
        for vm_id in targets:
            try:
                await asyncio.to_thread(sdk.stop, vm_id)
                affected.append(vm_id)
            except VMNotFoundError:
                logger.warning("VM %s not found, skipping.", vm_id)
            except Exception:
                logger.warning("Failed to stop VM %s", vm_id, exc_info=True)
        result_msg = f"Stopped {len(affected)} VMs."

    elif parsed.action == CommandAction.INFO:
        try:
            vm = await asyncio.to_thread(sm.get_vm, parsed.target)
            affected = [vm.vm_id]
            result_msg = f"VM {vm.vm_id}: {vm.status.value}"
        except VMNotFoundError:
            result_msg = f"VM not found: {parsed.target}"

    else:
        return JSONResponse(
            status_code=400,
            content={"error": f"Unknown command: {request.text}"},
        )

    return CommandResponse(
        action=parsed.action.value,
        target=parsed.target,
        result=result_msg,
        affected_vms=affected,
    )


def _resolve_targets(vms: list[VMInfo], target: str) -> list[str]:
    """Resolve a target string to a list of VM IDs.

    Handles:
    - "all" → all VMs
    - "error"/"running"/"stopped"/"created" → filter by status
    - specific VM ID → single VM (case-sensitive)
    """
    target = target.strip()
    target_lower = target.lower()

    if target_lower == "all":
        return [vm.vm_id for vm in vms]

    # Try as a status filter (case-insensitive)
    try:
        status = VMState(target_lower)
        return [vm.vm_id for vm in vms if vm.status == status]
    except ValueError:
        pass

    # Try as a specific VM ID (case-sensitive, exact match)
    for vm in vms:
        if vm.vm_id == target:
            return [vm.vm_id]

    return []


# =====================================================================
# WebSocket Endpoint
# =====================================================================


@app.websocket("/api/stream")
async def websocket_stream(websocket: WebSocket) -> None:
    """Real-time VM state updates via WebSocket.

    Clients receive JSON messages with types:
    - vm_created: New VM appeared
    - vm_updated: VM status changed
    - vm_deleted: VM removed
    """
    conn_mgr = _get_conn_manager(app)
    await conn_mgr.connect(websocket)
    try:
        # Send initial state snapshot
        sm = _get_state_manager(app)
        vms = await asyncio.to_thread(sm.list_vms)
        await conn_mgr.send_personal(
            websocket,
            {
                "type": "snapshot",
                "vms": [{"vm_id": vm.vm_id, "status": vm.status.value} for vm in vms],
            },
        )

        # Keep connection alive, listen for client messages
        while True:
            data = await websocket.receive_text()
            # Future: handle client-side commands via WebSocket
            logger.debug("Received WebSocket message: %s", data)

    except WebSocketDisconnect:
        conn_mgr.disconnect(websocket)
    except Exception:
        conn_mgr.disconnect(websocket)


# =====================================================================
# Static file serving (React build output)
# =====================================================================

_ui_dist = Path(__file__).parent / "ui" / "dist"
if _ui_dist.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=_ui_dist, html=True), name="ui")
    logger.info("Serving static UI from: %s", _ui_dist)
