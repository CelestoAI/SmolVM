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
import os
import shutil
import tarfile
import tempfile
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from smolvm.dashboard.commands import CommandAction, parse_command
from smolvm.dashboard.connection_manager import ConnectionManager
from smolvm.dashboard.poller import poll_vm_state
from smolvm.exceptions import VMNotFoundError
from smolvm.storage import StateManagerProtocol, create_state_manager
from smolvm.types import VMInfo, VMState
from smolvm.vm import SmolVMManager, resolve_data_dir

logger = logging.getLogger(__name__)

RELEASES_URL = "https://api.github.com/repos/CelestoAI/SmolVM/releases"
DASHBOARD_ASSET_PREFIX = "smolvm-dashboard-ui-"
DASHBOARD_ASSET_SUFFIX = ".tar.gz"
UI_DIST_ENV = "SMOLVM_DASHBOARD_UI_DIST"
ALLOW_BETA_ENV = "SMOLVM_DASHBOARD_ALLOW_BETA"
DASHBOARD_URL_ENV = "SMOLVM_DASHBOARD_URL"


def _resolve_ui_dist_path() -> Path:
    """Resolve where dashboard static files should be served from."""
    configured_path = os.environ.get(UI_DIST_ENV)
    if configured_path:
        return Path(configured_path).expanduser().resolve()

    server_dir = Path(__file__).resolve().parent
    repo_root = server_dir.parents[2]
    repo_ui_dir = repo_root / "ui"

    # Source checkout layout: <repo>/src/smolvm/dashboard/server.py + <repo>/ui/dist
    if repo_ui_dir.is_dir() or (repo_root / ".git").exists():
        return repo_ui_dir / "dist"

    # Installed package fallback: cache downloaded UI under writable SmolVM state.
    return resolve_data_dir() / "dashboard-ui" / "dist"


def _github_headers() -> dict[str, str]:
    """Build GitHub API headers with optional auth token for rate limits."""
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _allow_beta_releases() -> bool:
    """Return whether prerelease dashboard assets are allowed."""
    value = os.environ.get(ALLOW_BETA_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _latest_dashboard_release_asset(*, allow_prerelease: bool = False) -> tuple[str, str]:
    """Return newest release tag and dashboard dist asset download URL.

    Scans recent releases and picks the first release that has a dashboard UI
    asset. Prereleases are ignored by default unless ``allow_prerelease`` is true.
    """
    response = requests.get(
        RELEASES_URL,
        headers=_github_headers(),
        params={"per_page": 30},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, list):
        raise RuntimeError("Releases payload is not a list")

    for release in payload:
        if not isinstance(release, dict):
            continue
        if release.get("draft") is True:
            continue
        if release.get("prerelease") is True and not allow_prerelease:
            continue

        tag_name = release.get("tag_name")
        assets = release.get("assets")
        if not isinstance(tag_name, str) or not tag_name or not isinstance(assets, list):
            continue

        expected_name = f"{DASHBOARD_ASSET_PREFIX}{tag_name}{DASHBOARD_ASSET_SUFFIX}"
        fallback_url: str | None = None

        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = asset.get("name")
            url = asset.get("browser_download_url")
            if not isinstance(name, str) or not isinstance(url, str):
                continue

            if name == expected_name:
                return tag_name, url

            if (
                fallback_url is None
                and name.startswith(DASHBOARD_ASSET_PREFIX)
                and name.endswith(DASHBOARD_ASSET_SUFFIX)
            ):
                fallback_url = url

        if fallback_url is not None:
            return tag_name, fallback_url

    release_scope = "stable/prerelease" if allow_prerelease else "stable"
    raise RuntimeError(
        f"No {release_scope} release includes a dashboard UI asset "
        f"({DASHBOARD_ASSET_PREFIX}*{DASHBOARD_ASSET_SUFFIX})"
    )


def _format_bytes(size: int) -> str:
    """Format bytes as a compact human-readable string."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _download_asset(url: str, destination: Path) -> None:
    """Download a release asset to disk with progress logging."""
    logger.info("Downloading dashboard UI bundle from: %s", url)
    with requests.get(url, headers=_github_headers(), stream=True, timeout=60) as response:
        response.raise_for_status()

        total_bytes: int | None = None
        content_length = response.headers.get("content-length")
        if content_length and content_length.isdigit():
            total_bytes = int(content_length)
            logger.info("Dashboard UI bundle size: %s", _format_bytes(total_bytes))

        downloaded = 0
        next_progress_log = 5 * 1024 * 1024

        with destination.open("wb") as out:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue

                out.write(chunk)
                downloaded += len(chunk)

                if downloaded >= next_progress_log:
                    if total_bytes:
                        percent = min(100.0, (downloaded / total_bytes) * 100.0)
                        logger.info(
                            "Dashboard UI download progress: %s / %s (%.0f%%)",
                            _format_bytes(downloaded),
                            _format_bytes(total_bytes),
                            percent,
                        )
                    else:
                        logger.info(
                            "Dashboard UI download progress: %s",
                            _format_bytes(downloaded),
                        )
                    next_progress_log += 5 * 1024 * 1024

    if total_bytes:
        logger.info(
            "Dashboard UI download complete: %s / %s",
            _format_bytes(downloaded),
            _format_bytes(total_bytes),
        )
    else:
        logger.info("Dashboard UI download complete: %s", _format_bytes(downloaded))


def _extract_dashboard_dist(archive_path: Path, extract_dir: Path) -> Path:
    """Extract dashboard archive and return the extracted dist directory."""
    with tarfile.open(archive_path, "r:gz") as tar:
        for member in tar.getmembers():
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise RuntimeError(f"Unsafe path in dashboard archive: {member.name}")

        if hasattr(tarfile, "data_filter"):
            tar.extractall(path=extract_dir, filter="data")
        else:
            # Python < 3.12 without PEP 706 backport — fall back to
            # unfiltered extraction.  The path validation above guards
            # against absolute/.. paths; tar.filter is defense-in-depth.
            tar.extractall(path=extract_dir)  # noqa: S202

    dist_dir = extract_dir / "dist"
    if dist_dir.is_dir() and (dist_dir / "index.html").is_file():
        return dist_dir

    for candidate in extract_dir.rglob("dist"):
        if candidate.is_dir() and (candidate / "index.html").is_file():
            return candidate

    raise RuntimeError("Dashboard archive did not contain dist/index.html")


def _ensure_latest_dashboard_ui_dist(target_dist: Path, *, allow_prerelease: bool = False) -> bool:
    """Download latest dashboard dist release asset and place it at target_dist."""
    target_root = target_dist.parent
    tag_file = target_root / ".dashboard-ui-tag"

    try:
        latest_tag, asset_url = _latest_dashboard_release_asset(allow_prerelease=allow_prerelease)
    except Exception:
        logger.warning("Failed to discover latest dashboard UI release asset", exc_info=True)
        return False

    if target_dist.is_dir() and (target_dist / "index.html").is_file() and tag_file.is_file():
        try:
            if tag_file.read_text(encoding="utf-8").strip() == latest_tag:
                logger.info("Dashboard UI already up to date (tag=%s)", latest_tag)
                return True
        except OSError as exc:
            # If we cannot read the tag file, treat it as a cache miss and re-download.
            logger.debug("Failed to read dashboard UI tag file %s: %s", tag_file, exc)

    try:
        logger.info("Preparing dashboard UI bundle (target tag=%s)", latest_tag)
        target_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="smolvm-ui-", dir=str(target_root)) as tmp_dir:
            tmp_path = Path(tmp_dir)
            archive_path = tmp_path / "dashboard-ui.tar.gz"
            extract_dir = tmp_path / "extract"
            extract_dir.mkdir(parents=True, exist_ok=True)

            _download_asset(asset_url, archive_path)
            extracted_dist = _extract_dashboard_dist(archive_path, extract_dir)

            staged_dist = tmp_path / "dist"
            shutil.copytree(extracted_dist, staged_dist)

            if target_dist.exists():
                if target_dist.is_dir():
                    shutil.rmtree(target_dist)
                else:
                    target_dist.unlink()
            shutil.move(str(staged_dist), str(target_dist))
            logger.info("Dashboard UI bundle extracted to: %s", target_dist)

        try:
            tag_file.write_text(f"{latest_tag}\n", encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to update dashboard UI tag file %s: %s", tag_file, exc)

        return True

    except Exception:
        logger.warning("Failed to download/extract/store dashboard UI dist", exc_info=True)
        return False


# --- Accessor helpers for app.state ---
def _get_sdk(app: FastAPI) -> SmolVMManager:
    """Get the SDK instance from app.state, raising if not initialized."""
    sdk: SmolVMManager | None = getattr(app.state, "sdk", None)
    if sdk is None:
        raise RuntimeError("SmolVMManager not initialized.")
    return sdk


def _get_state_manager(app: FastAPI) -> StateManagerProtocol:
    """Get the StateManager instance from app.state, raising if not initialized."""
    sm: StateManagerProtocol | None = getattr(app.state, "state_manager", None)
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

    allow_beta = _allow_beta_releases()
    if await asyncio.to_thread(
        _ensure_latest_dashboard_ui_dist,
        _ui_dist,
        allow_prerelease=allow_beta,
    ):
        logger.info(
            "Dashboard UI dist ready at: %s (allow_beta=%s)",
            _ui_dist,
            allow_beta,
        )
    elif _ui_dist.is_dir():
        logger.warning(
            "Using existing dashboard UI dist at: %s (allow_beta=%s)",
            _ui_dist,
            allow_beta,
        )
    else:
        logger.warning(
            "Dashboard UI dist unavailable at startup: %s (allow_beta=%s)",
            _ui_dist,
            allow_beta,
        )

    app.state.state_manager = create_state_manager(db_path=db_path)
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
    dashboard_url = os.environ.get(DASHBOARD_URL_ENV)
    if dashboard_url:
        logger.info("Open %s to view the dashboard", dashboard_url)
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
            "memory": vm.config.memory,
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


def _resolve_ssh_key_path() -> str | None:
    """Resolve the SmolVM SSH private key for guest connections."""
    home = Path.home()
    candidates = [
        home / ".smolvm" / "keys" / "id_ed25519",
        home / ".smolvm" / "id_ed25519",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


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
        status: Filter by VM status (created/running/paused/stopped/error).
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


@app.get("/api/vms/{vm_id}/processes")
async def get_vm_processes(vm_id: str) -> dict[str, Any]:
    """Get running processes inside a VM via SSH.

    Connects to the guest over SSH, runs ``ps``, and returns a structured
    list of processes.  The VM must be in the *running* state.
    """
    sm = _get_state_manager(app)
    try:
        vm_info = await asyncio.to_thread(sm.get_vm, vm_id)
    except VMNotFoundError:
        raise HTTPException(status_code=404, detail=f"VM not found: {vm_id}") from None

    if vm_info.status != VMState.RUNNING:
        raise HTTPException(
            status_code=409,
            detail=f"VM is not running (status: {vm_info.status.value})",
        )

    if vm_info.network is None:
        raise HTTPException(status_code=409, detail="VM has no network configuration")

    network = vm_info.network  # capture for the thread closure

    def _fetch() -> list[dict[str, str]]:
        from smolvm.ssh import SSHClient

        # Prefer localhost forwarded port when available.
        if network.ssh_host_port:
            host = "127.0.0.1"
            port = network.ssh_host_port
        else:
            host = network.guest_ip
            port = 22

        key_path = _resolve_ssh_key_path()
        client = SSHClient(host=host, port=port, key_path=key_path, connect_timeout=5)
        try:
            result = client.run(
                "ps -eo pid,user,vsz,stat,args",
                timeout=10,
                shell="raw",
            )
            if result.exit_code != 0:
                return []

            lines = result.stdout.strip().splitlines()
            # Skip header line
            if len(lines) <= 1:
                return []

            processes: list[dict[str, str]] = []
            for line in lines[1:]:
                parts = line.split(None, 4)
                if len(parts) >= 4:
                    processes.append(
                        {
                            "pid": parts[0],
                            "user": parts[1],
                            "vsz": parts[2],
                            "stat": parts[3],
                            "command": parts[4] if len(parts) > 4 else "",
                        }
                    )
            return processes
        finally:
            client.close()

    try:
        processes = await asyncio.to_thread(_fetch)
    except Exception as e:
        logger.warning("Failed to fetch processes for VM %s: %s", vm_id, e)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to retrieve processes from VM: {e}",
        ) from None

    return {"vm_id": vm_id, "processes": processes}


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
    - "error"/"running"/"paused"/"stopped"/"created" → filter by status
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

_ui_dist = _resolve_ui_dist_path()
try:
    _ui_dist.mkdir(parents=True, exist_ok=True)
except OSError:
    logger.warning("Failed to prepare dashboard UI dist directory: %s", _ui_dist, exc_info=True)

app.mount("/", StaticFiles(directory=_ui_dist, html=True, check_dir=False), name="ui")

if _ui_dist.is_dir():
    logger.info("Serving dashboard UI from: %s", _ui_dist)
else:
    logger.warning("Dashboard UI dist directory not found at import: %s", _ui_dist)
