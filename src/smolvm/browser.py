"""Browser-session orchestration on top of SmolVM guests."""

from __future__ import annotations

import hashlib
import logging
import platform
import shlex
import socket
import uuid
import webbrowser
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from smolvm.exceptions import BrowserSessionNotFoundError, SmolVMError
from smolvm.facade import SmolVM
from smolvm.runtime.backends import BACKEND_QEMU, resolve_backend
from smolvm.runtime.boot_profiles import (
    KernelBootProfile,
    get_boot_profile_spec,
)
from smolvm.storage import StateManagerProtocol, create_state_manager
from smolvm.types import (
    BrowserSessionConfig,
    BrowserSessionInfo,
    BrowserSessionState,
    PortForwardConfig,
    VMConfig,
    VMState,
)
from smolvm.vm import resolve_data_dir

logger = logging.getLogger(__name__)

_BROWSER_DEBUG_PORT = 9222
_BROWSER_LIVE_PORT = 6080
_DEFAULT_BROWSER_BOOT_TIMEOUT = 90.0
_BROWSER_GUEST_ROOT = "/opt/smolvm-browser"
_BROWSER_GUEST_PROFILE_ROOT = f"{_BROWSER_GUEST_ROOT}/profiles"
_BROWSER_GUEST_DOWNLOAD_ROOT = f"{_BROWSER_GUEST_ROOT}/downloads"
_BROWSER_GUEST_ARTIFACT_ROOT = f"{_BROWSER_GUEST_ROOT}/artifacts"
_BROWSER_GUEST_LOG_ROOT = "/var/log/smolvm-browser"
_BROWSER_KERNEL_PROFILE = KernelBootProfile.MICROVM_DIRECT


def _generate_browser_session_id() -> str:
    """Generate a browser session identifier."""
    return f"browser-{uuid.uuid4().hex[:8]}"


def _browser_state_manager(data_dir: Path | None = None) -> StateManagerProtocol:
    """Return a state manager bound to the resolved SmolVM data dir."""
    resolved = resolve_data_dir(data_dir)
    return create_state_manager(db_path=resolved / "smolvm.db")


def _stable_browser_vm_id(profile_id: str) -> str:
    """Derive a stable VM identifier for a persistent profile."""
    normalized = "".join(
        char if char.isalnum() or char in {"-", "_"} else "-" for char in profile_id
    )
    normalized = normalized.strip("-_").lower() or "profile"
    digest = hashlib.sha1(profile_id.encode("utf-8")).hexdigest()[:8]
    max_slug_len = 63 - len("browser-prof--") - len(digest)
    slug = normalized[:max_slug_len]
    return f"browser-prof-{slug}-{digest}"


def _browser_vm_id(session_id: str, config: BrowserSessionConfig) -> str:
    """Resolve the underlying VM identifier for a browser session."""
    if config.profile_mode == "persistent":
        assert config.profile_id is not None
        return _stable_browser_vm_id(config.profile_id)
    return session_id


def _allocate_browser_host_port(exclude: set[int] | None = None) -> int:
    """Allocate an available localhost TCP port for browser forwarding."""
    excluded = set(exclude or ())
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port not in excluded:
            return port
    raise SmolVMError("Failed to allocate a localhost port for browser forwarding.")


def _qemu_browser_port_forwards(config: BrowserSessionConfig) -> list[PortForwardConfig]:
    """Return stable QEMU host forwards for browser endpoints."""
    reserved: set[int] = set()
    debug_port = _allocate_browser_host_port(reserved)
    reserved.add(debug_port)
    forwards = [PortForwardConfig(host_port=debug_port, guest_port=_BROWSER_DEBUG_PORT)]
    if config.mode == "live":
        live_port = _allocate_browser_host_port(reserved)
        forwards.append(PortForwardConfig(host_port=live_port, guest_port=_BROWSER_LIVE_PORT))
    return forwards


def _browser_boot_args_for_backend(backend: str) -> str:
    """Return backend-specific kernel boot arguments for browser images."""
    arch = platform.machine().lower()
    profile = get_boot_profile_spec(_BROWSER_KERNEL_PROFILE)
    return profile.base_boot_args_for_backend(backend, arch)


def _build_browser_vm_config(
    *,
    session_id: str,
    browser_config: BrowserSessionConfig,
    ssh_key_path: str | None = None,
) -> tuple[VMConfig, str | None]:
    """Build the underlying VM config for a browser session."""
    from smolvm.images.builder import ImageBuilder
    from smolvm.utils import ensure_ssh_key

    resolved_backend = resolve_backend(browser_config.backend)
    private_key, public_key = ensure_ssh_key()
    resolved_ssh_key_path = ssh_key_path or str(private_key)

    builder = ImageBuilder()
    # TODO: Make the browser runtime pluggable so BrowserSessionConfig.browser
    # can select alternative engines (for example Lightpanda) without changing
    # the surrounding session lifecycle or backend abstractions.
    image_arch = "aarch64" if platform.machine().lower() in {"arm64", "aarch64"} else "x86_64"
    image_name = f"browser-chromium-{image_arch}"
    port_forwards: list[PortForwardConfig] = []
    if resolved_backend == BACKEND_QEMU:
        image_name = f"{image_name}-qemu"
        port_forwards = _qemu_browser_port_forwards(browser_config)
    if browser_config.disk_size_mib != 4096:
        image_name = f"{image_name}-{browser_config.disk_size_mib}m"

    kernel, rootfs = builder.build_browser_rootfs(
        public_key,
        name=image_name,
        rootfs_size_mb=browser_config.disk_size_mib,
        kernel_profile=_BROWSER_KERNEL_PROFILE,
    )

    config = VMConfig(
        vm_id=_browser_vm_id(session_id, browser_config),
        vcpu_count=1,
        memory=browser_config.mem_size_mib,
        kernel_path=kernel,
        rootfs_path=rootfs,
        boot_args=_browser_boot_args_for_backend(resolved_backend),
        backend=resolved_backend,
        retain_disk_on_delete=browser_config.profile_mode == "persistent",
        env_vars=browser_config.env_vars,
        port_forwards=port_forwards,
        ssh_public_key=public_key.read_text().strip(),
    )
    return config, resolved_ssh_key_path


class BrowserSession:
    """Disposable browser session running inside a SmolVM guest."""

    def __init__(
        self,
        config: BrowserSessionConfig | None = None,
        *,
        session_id: str | None = None,
        data_dir: Path | None = None,
        socket_dir: Path | None = None,
        ssh_key_path: str | None = None,
    ) -> None:
        if config is not None and session_id is not None:
            raise ValueError("Provide either config or session_id, not both.")

        self._data_dir = resolve_data_dir(data_dir)
        self._socket_dir = socket_dir
        self._ssh_key_path = ssh_key_path
        self._state = _browser_state_manager(self._data_dir)
        self._owns_session = False
        self._vm: SmolVM | None = None
        self._playwright_runtime: Any | None = None

        if config is None and session_id is None:
            config = BrowserSessionConfig()

        if config is not None:
            self._init_new_session(config)
        else:
            assert session_id is not None
            self._attach_existing_session(session_id)

    @classmethod
    def from_id(
        cls,
        session_id: str,
        *,
        data_dir: Path | None = None,
        socket_dir: Path | None = None,
        ssh_key_path: str | None = None,
    ) -> BrowserSession:
        """Reconnect to an existing browser session by session ID."""
        return cls(
            session_id=session_id,
            data_dir=data_dir,
            socket_dir=socket_dir,
            ssh_key_path=ssh_key_path,
        )

    def _init_new_session(self, config: BrowserSessionConfig) -> None:
        session_id = config.session_id or _generate_browser_session_id()
        session_config = (
            config if config.session_id else config.model_copy(update={"session_id": session_id})
        )
        artifacts_dir = self._session_artifacts_dir(session_id)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=session_config.timeout_minutes)

        vm: SmolVM | None = None
        try:
            vm_config, resolved_ssh_key_path = _build_browser_vm_config(
                session_id=session_id,
                browser_config=session_config,
                ssh_key_path=self._ssh_key_path,
            )
            self._ssh_key_path = resolved_ssh_key_path
            vm = SmolVM(
                vm_config,
                data_dir=self._data_dir,
                socket_dir=self._socket_dir,
                ssh_key_path=self._ssh_key_path,
            )
            info = BrowserSessionInfo(
                session_id=session_id,
                vm_id=vm.vm_id,
                status=BrowserSessionState.CREATED,
                profile_id=session_config.profile_id,
                expires_at=expires_at,
                artifacts_dir=artifacts_dir,
            )
            self._state.create_browser_session(info, session_config)
        except Exception:
            if vm is not None:
                with suppress(Exception):
                    vm.delete()
                with suppress(Exception):
                    vm.close()
            raise

        self._session_config = session_config
        self._info = info
        self._vm = vm
        self._owns_session = True

    def _attach_existing_session(self, session_id: str) -> None:
        self._info = self._state.get_browser_session(session_id)
        self._session_config = self._state.get_browser_session_config(session_id)

        try:
            self._vm = SmolVM.from_id(
                self._info.vm_id,
                data_dir=self._data_dir,
                socket_dir=self._socket_dir,
                ssh_key_path=self._ssh_key_path,
            )
        except Exception:
            logger.warning(
                "Browser session %s could not attach to VM %s",
                session_id,
                self._info.vm_id,
                exc_info=True,
            )
            self._vm = None

    @property
    def session_id(self) -> str:
        """Stable browser session identifier."""
        return self._info.session_id

    @property
    def vm_id(self) -> str:
        """Underlying SmolVM identifier."""
        return self._info.vm_id

    @property
    def info(self) -> BrowserSessionInfo:
        """Current browser session info."""
        return self._info

    @property
    def status(self) -> BrowserSessionState:
        """Current browser session status."""
        return self._info.status

    @property
    def cdp_url(self) -> str | None:
        """HTTP CDP endpoint exposed on localhost."""
        return self._info.cdp_url

    @property
    def live_url(self) -> str | None:
        """Optional live-view URL exposed on localhost."""
        return self._info.live_url

    @property
    def artifacts_dir(self) -> Path | None:
        """Local host directory for collected session artifacts."""
        return self._info.artifacts_dir

    @property
    def config(self) -> BrowserSessionConfig:
        """Resolved browser session configuration."""
        return self._session_config

    @property
    def data_dir(self) -> Path:
        """SmolVM data directory used by this session."""
        return self._data_dir

    def refresh(self) -> BrowserSession:
        """Refresh session info from persisted state."""
        self._info = self._state.get_browser_session(self._info.session_id)
        return self

    def start(self, boot_timeout: float = _DEFAULT_BROWSER_BOOT_TIMEOUT) -> BrowserSession:
        """Start the browser session and expose its CDP/live endpoints."""
        if self._session_config.network_policy_id is not None:
            raise SmolVMError(
                "network_policy_id is reserved for future host-side policy enforcement "
                "and is not implemented yet."
            )
        if self._vm is None:
            raise SmolVMError(
                f"Browser session '{self.session_id}' cannot be started because "
                "its VM is unavailable."
            )

        self._info = self._state.update_browser_session(
            self.session_id,
            status=BrowserSessionState.STARTING,
        )

        try:
            if self._vm.status in {VMState.CREATED, VMState.STOPPED}:
                self._vm.start(boot_timeout=boot_timeout)
            self._vm.wait_for_ssh(timeout=boot_timeout)

            debug_ready = self._wait_for_guest_port(_BROWSER_DEBUG_PORT, timeout=1.0)
            live_ready = (
                self._wait_for_guest_port(_BROWSER_LIVE_PORT, timeout=1.0)
                if self._session_config.mode == "live"
                else True
            )
            if not debug_ready or not live_ready:
                self._start_guest_browser()

            if not self._wait_for_guest_port(_BROWSER_DEBUG_PORT, timeout=boot_timeout):
                raise SmolVMError(
                    f"Browser session '{self.session_id}' did not expose a CDP port in time."
                )
            debug_host_port = self._resolve_browser_host_port(_BROWSER_DEBUG_PORT)
            cdp_url = f"http://127.0.0.1:{debug_host_port}"

            live_url: str | None = None
            if self._session_config.mode == "live":
                if not self._wait_for_guest_port(_BROWSER_LIVE_PORT, timeout=boot_timeout):
                    raise SmolVMError(
                        f"Browser session '{self.session_id}' did not expose a live view in time."
                    )
                live_host_port = self._resolve_browser_host_port(_BROWSER_LIVE_PORT)
                live_url = f"http://127.0.0.1:{live_host_port}/vnc.html?autoconnect=1&resize=scale"

            self._info = self._state.update_browser_session(
                self.session_id,
                status=BrowserSessionState.READY,
                cdp_url=cdp_url,
                live_url=live_url,
                debug_port=debug_host_port,
                profile_id=self._session_config.profile_id,
                expires_at=self._info.expires_at,
                artifacts_dir=self._session_artifacts_dir(self.session_id),
                config=self._session_config,
            )
            return self
        except Exception:
            self._info = self._state.update_browser_session(
                self.session_id,
                status=BrowserSessionState.ERROR,
            )
            raise

    def stop(self) -> BrowserSession:
        """Stop the browser session and tear down its VM."""
        with suppress(BrowserSessionNotFoundError):
            self._info = self._state.update_browser_session(
                self.session_id,
                status=BrowserSessionState.STOPPING,
            )

        if self._vm is not None:
            if self._vm.status == VMState.RUNNING:
                with suppress(Exception):
                    self._vm.run("/usr/local/bin/smolvm-browser-session stop", timeout=30)
                with suppress(Exception):
                    self.collect_artifacts()
            with suppress(Exception):
                self._vm.delete()

        with suppress(BrowserSessionNotFoundError):
            self._state.delete_browser_session(self.session_id)

        self.close()
        return self

    def delete(self) -> None:
        """Alias for stop() to match the VM facade naming."""
        self.stop()

    def connect_playwright(self) -> Any:
        """Connect Playwright to the browser session over CDP."""
        if self._info.cdp_url is None:
            raise SmolVMError("Browser session is not ready; start it before connecting.")

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise SmolVMError(
                "Playwright is not installed. Install it with: pip install playwright"
            ) from e

        if self._playwright_runtime is None:
            self._playwright_runtime = sync_playwright().start()

        return self._playwright_runtime.chromium.connect_over_cdp(self._info.cdp_url)

    def screenshot(
        self,
        destination: str | Path,
        *,
        full_page: bool = True,
    ) -> Path:
        """Capture a screenshot via Playwright and save it locally."""
        browser = self.connect_playwright()
        contexts = browser.contexts
        context = contexts[0] if contexts else browser.new_context()

        pages = context.pages
        page = pages[0] if pages else context.new_page()

        output_path = Path(destination)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output_path), full_page=full_page)
        return output_path

    def open_live_view(self) -> bool:
        """Open the live-view URL in the local default browser."""
        if self._info.live_url is None:
            raise SmolVMError("This browser session does not expose a live_url.")
        return webbrowser.open(self._info.live_url)

    def push_file(self, local_path: str | Path, guest_path: str) -> None:
        """Upload a file into the guest using the session SSH channel."""
        if self._vm is None:
            raise SmolVMError("Browser session VM is unavailable.")
        ssh = self._vm._ensure_ssh_for_env()
        ssh.put_file(local_path, guest_path)

    def pull_file(self, guest_path: str, local_path: str | Path) -> Path:
        """Download a file from the guest using the session SSH channel."""
        if self._vm is None:
            raise SmolVMError("Browser session VM is unavailable.")
        ssh = self._vm._ensure_ssh_for_env()
        return ssh.get_file(guest_path, local_path)

    def collect_artifacts(self) -> Path | None:
        """Collect guest logs/downloads/recordings into the local artifacts dir."""
        if self._vm is None or self._vm.status != VMState.RUNNING:
            return None

        artifacts_dir = self._session_artifacts_dir(self.session_id)
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        remote_archive = f"/tmp/{self.session_id}-artifacts.tar.gz"
        guest_artifacts = self._guest_artifacts_dir()
        guest_downloads = self._guest_download_dir()
        quoted_archive = shlex.quote(remote_archive)
        artifact_target = shlex.quote(guest_artifacts)
        download_target = shlex.quote(guest_downloads)
        log_target = shlex.quote(_BROWSER_GUEST_LOG_ROOT)
        command = (
            f"ARCHIVE={quoted_archive}; "
            'TARGETS=""; '
            f'[ -d {artifact_target} ] && TARGETS="$TARGETS {artifact_target}"; '
            f'[ -d {download_target} ] && TARGETS="$TARGETS {download_target}"; '
            f'[ -d {log_target} ] && TARGETS="$TARGETS {log_target}"; '
            'rm -f "$ARCHIVE"; '
            'if [ -z "$TARGETS" ]; then exit 0; fi; '
            'tar -czf "$ARCHIVE" $TARGETS'
        )
        result = self._vm.run(command, timeout=180)
        if not result.ok:
            raise SmolVMError(
                "Failed to collect browser session artifacts: "
                f"{result.stderr.strip() or result.stdout}"
            )

        archive_path = artifacts_dir / "guest-artifacts.tar.gz"
        ssh = self._vm._ensure_ssh_for_env()
        return ssh.get_file(remote_archive, archive_path)

    def logs(self, tail: int = 100) -> str:
        """Return combined host/guest logs for the browser session."""
        chunks: list[str] = []

        host_log = self.data_dir / f"{self.vm_id}.log"
        if host_log.exists():
            chunks.append(f"== {host_log} ==\n{self._tail_local_file(host_log, tail)}")

        if self._vm is not None and self._vm.status == VMState.RUNNING:
            command = (
                "for file in /var/log/smolvm-browser/*.log; do "
                '[ -f "$file" ] || continue; '
                f'echo "== $file =="; tail -n {tail} "$file"; '
                "echo; "
                "done"
            )
            result = self._vm.run(command, timeout=60)
            if result.stdout.strip():
                chunks.append(result.stdout.strip())

        return "\n\n".join(chunks).strip()

    def close(self) -> None:
        """Release local resources without changing the remote session state."""
        if self._playwright_runtime is not None:
            with suppress(Exception):
                self._playwright_runtime.stop()
            self._playwright_runtime = None

        if self._vm is not None:
            self._vm.close()
            self._vm = None

    def __enter__(self) -> BrowserSession:
        if self._owns_session and self._info.status != BrowserSessionState.READY:
            self.start()
        return self

    def __exit__(self, *args: object) -> None:
        if self._owns_session:
            with suppress(Exception):
                self.stop()
        self.close()

    def _start_guest_browser(self) -> None:
        if self._vm is None:
            raise SmolVMError("Browser session VM is unavailable.")

        command = " ".join(
            [
                "/usr/local/bin/smolvm-browser-session",
                "start",
                shlex.quote(self._session_config.mode),
                str(self._session_config.viewport_width),
                str(self._session_config.viewport_height),
                str(_BROWSER_DEBUG_PORT),
                str(_BROWSER_LIVE_PORT),
                shlex.quote(self._guest_profile_dir()),
                shlex.quote(self._guest_download_dir()),
                "1" if self._session_config.record_video else "0",
                "1" if self._session_config.allow_downloads else "0",
                shlex.quote(self._guest_artifacts_dir()),
            ]
        )
        result = self._vm.run(command, timeout=60)
        if not result.ok:
            raise SmolVMError(
                f"Failed to launch guest browser: {result.stderr.strip() or result.stdout}"
            )

    def _resolve_browser_host_port(self, guest_port: int) -> int:
        """Return a localhost port that exposes a browser guest port.

        Browser services such as Chromium's DevTools endpoint can bind guest
        loopback only. Route them through ``expose_local()`` so SmolVM can
        fall back to an SSH tunnel when direct guest networking is not enough.
        """
        if self._vm is None:
            raise SmolVMError("Browser session VM is unavailable.")

        return self._vm.expose_local(guest_port=guest_port)

    def _wait_for_guest_port(self, port: int, *, timeout: float) -> bool:
        if self._vm is None:
            raise SmolVMError("Browser session VM is unavailable.")

        command = f"/usr/local/bin/smolvm-browser-wait-port {port} {timeout}"
        result = self._vm.run(command, timeout=max(5, int(timeout) + 5))
        return result.ok

    def _session_artifacts_dir(self, session_id: str) -> Path:
        return self._data_dir / "browser-sessions" / session_id

    def _guest_profile_dir(self) -> str:
        key = self._session_config.profile_id or self.session_id
        return f"{_BROWSER_GUEST_PROFILE_ROOT}/{key}"

    def _guest_download_dir(self) -> str:
        return f"{_BROWSER_GUEST_DOWNLOAD_ROOT}/{self.session_id}"

    def _guest_artifacts_dir(self) -> str:
        return f"{_BROWSER_GUEST_ARTIFACT_ROOT}/{self.session_id}"

    @staticmethod
    def _tail_local_file(path: Path, line_count: int) -> str:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            return f"<failed to read {path}: {e}>"
        if line_count <= 0:
            return ""
        return "\n".join(lines[-line_count:])
