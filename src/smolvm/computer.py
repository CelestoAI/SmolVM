"""First-class Linux computer sessions built on the graphical VM runtime."""

from __future__ import annotations

import shlex
import tempfile
import threading
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Literal

from smolvm.browser import _BrowserSandbox
from smolvm.exceptions import BrowserSessionNotFoundError, SmolVMError
from smolvm.types import BrowserSessionState, CommandResult, ComputerEvent, VMState

_HEALTH_CHECK_INTERVAL_SECONDS = 2.0


class ComputerDisplay:
    """Connection addresses for watching or controlling one computer display."""

    def __init__(self, computer: _ComputerSandbox) -> None:
        self._computer = computer

    @property
    def viewer_url(self) -> str:
        value = self._computer.viewer_url
        if value is None:
            raise SmolVMError(
                f"Computer '{self._computer.computer_id}' has no viewer; "
                "delete it and create another."
            )
        return value

    @property
    def vnc_url(self) -> str:
        value = self._computer.display_url
        if value is None:
            raise SmolVMError(
                f"Computer '{self._computer.computer_id}' has no VNC display; "
                "delete it and create another."
            )
        return value


class ComputerBrowser:
    """Chromium application running inside a Linux computer."""

    def __init__(self, computer: _ComputerSandbox) -> None:
        self._computer = computer

    @property
    def status(self) -> Literal["ready", "closed", "error"]:
        if self._computer.status == BrowserSessionState.ERROR:
            return "error"
        if self._computer.status != BrowserSessionState.READY:
            return "closed"
        url = self._computer.cdp_url
        if url is None:
            return "closed"
        return "ready" if self._computer._wait_for_cdp_http(url, timeout=0.25) else "closed"

    @property
    def cdp_url(self) -> str | None:
        return self._computer.cdp_url if self.status == "ready" else None

    def launch(self) -> None:
        """Start Chromium when it was closed, without replacing the computer."""
        if self.status == "ready":
            return
        self._computer._launch_guest_browser()
        url = self._computer.cdp_url
        if url is None or not self._computer._wait_for_cdp_http(url, timeout=30):
            raise SmolVMError(
                f"Chromium did not open in computer '{self._computer.computer_id}'; "
                "inspect the computer logs and try computer.browser.launch() again."
            )

    def connect_playwright(self):  # type: ignore[no-untyped-def]
        """Connect Playwright to Chromium in this computer."""
        self.launch()
        return self._computer.connect_playwright()


class ComputerFiles:
    """Read and write files as the desktop's unprivileged agent user."""

    def __init__(self, computer: _ComputerSandbox) -> None:
        self._computer = computer

    @staticmethod
    def _path(path: str) -> PurePosixPath:
        resolved = PurePosixPath(path)
        if not resolved.is_absolute():
            raise ValueError("Computer file paths must be absolute, such as '/workspace/file.txt'.")
        return resolved

    def read(self, path: str, *, max_bytes: int | None = None) -> bytes:
        guest_path = self._path(path)
        guest_temporary = f"/tmp/smolvm-computer-read-{uuid.uuid4().hex}"
        with tempfile.NamedTemporaryFile(prefix="smolvm-computer-read-", delete=False) as handle:
            local_path = Path(handle.name)
        try:
            stage_command = "runuser -u agent -- sh -c " + shlex.quote(
                f"install -m 0600 -- {shlex.quote(str(guest_path))} {shlex.quote(guest_temporary)}"
            )
            staged = self._computer.vm.run(stage_command, timeout=30, shell="raw")
            if not staged.ok:
                raise SmolVMError(
                    f"Could not read '{guest_path}' as the computer user; "
                    "choose a readable file and retry."
                )
            self._computer.vm.download_file(guest_temporary, local_path, max_bytes=max_bytes)
            return local_path.read_bytes()
        finally:
            local_path.unlink(missing_ok=True)
            with suppress(Exception):
                self._computer.vm.run(
                    f"rm -f -- {shlex.quote(guest_temporary)}",
                    timeout=10,
                    shell="raw",
                )

    def write(self, path: str, content: str | bytes) -> None:
        guest_path = self._path(path)
        payload = content.encode() if isinstance(content, str) else content
        local_path: Path | None = None
        guest_temporary = f"/tmp/smolvm-computer-{uuid.uuid4().hex}"
        destination_temporary = f"{guest_path}.smolvm-{uuid.uuid4().hex}.tmp"
        try:
            with tempfile.NamedTemporaryFile(
                prefix="smolvm-computer-write-", delete=False
            ) as handle:
                local_path = Path(handle.name)
                handle.write(payload)
            self._computer.vm.upload_file(local_path, guest_temporary)
            destination = shlex.quote(str(guest_path))
            destination_tmp = shlex.quote(destination_temporary)
            source = shlex.quote(guest_temporary)
            command = "runuser -u agent -- sh -c " + shlex.quote(
                f"mkdir -p -- {shlex.quote(str(guest_path.parent))} && "
                f"install -m 0644 -- {source} {destination_tmp} && "
                f"mv -f -- {destination_tmp} {destination}"
            )
            result = self._computer.vm.run(command, timeout=30, shell="raw")
            if not result.ok:
                raise SmolVMError(
                    f"Could not write '{guest_path}' as the computer user; "
                    "choose a writable path such as '/workspace/file.txt'."
                )
        finally:
            if local_path is not None:
                local_path.unlink(missing_ok=True)
            with suppress(Exception):
                self._computer.vm.run(f"rm -f -- {shlex.quote(guest_temporary)}", timeout=10)
            with suppress(Exception):
                self._computer.run(
                    f"rm -f -- {shlex.quote(destination_temporary)}",
                    timeout=10,
                    shell="raw",
                )


class _ComputerSandbox(_BrowserSandbox):
    """A complete Linux desktop with one managed Chromium application."""

    template = "linux-desktop"
    capabilities = (
        "display.viewer",
        "display.vnc",
        "browser.cdp",
        "sandbox.exec",
        "sandbox.files",
    )

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self._event_callback: Callable[[ComputerEvent], None] | None = None
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        super().__init__(*args, **kwargs)
        if self._session_config.mode != "computer":
            self.close()
            raise ValueError(
                f"'{self.session_id}' is not a computer; run 'smolvm computer list' to choose one."
            )
        self.display = ComputerDisplay(self)
        self.browser = ComputerBrowser(self)
        self.files = ComputerFiles(self)

    @property
    def computer_id(self) -> str:
        return self.session_id

    @property
    def sandbox_id(self) -> str:
        return self.vm_id

    def run(
        self,
        command: str,
        timeout: int | float | None = None,
        shell: Literal["login", "raw"] = "login",
    ) -> CommandResult:
        """Run one command as the same user that owns the desktop."""
        if shell == "raw":
            wrapped = f"runuser -u agent -- sh -c {shlex.quote(command)}"
        else:
            wrapped = f"runuser -u agent -- sh -lc {shlex.quote(command)}"
        return self.vm.run(wrapped, timeout=30 if timeout is None else timeout, shell="raw")

    def enable_events(self, callback: Callable[[ComputerEvent], None] | None) -> None:
        """Publish lifecycle events and watch the required desktop processes."""
        self._event_callback = callback
        self._emit_event(
            {
                "type": "computer.ready",
                "computer_id": self.computer_id,
                "sandbox_id": self.sandbox_id,
            }
        )
        if callback is None:
            return
        self._monitor_thread = threading.Thread(
            target=self._monitor_required_processes,
            name=f"smolvm-computer-health-{self.computer_id}",
            daemon=True,
        )
        self._monitor_thread.start()

    def _emit_event(self, event: ComputerEvent) -> None:
        callback = getattr(self, "_event_callback", None)
        if callback is not None:
            with suppress(Exception):
                callback(event)

    def _failed_required_process(self) -> str | None:
        command = (
            "for process in Xvfb openbox x11vnc websockify; do "
            'pgrep -x "$process" >/dev/null || { printf \'%s\\n\' "$process"; exit; }; '
            "done"
        )
        result = self.vm.run(command, timeout=10, shell="raw")
        return result.stdout.strip() or None

    def _monitor_required_processes(self) -> None:
        while not self._monitor_stop.wait(_HEALTH_CHECK_INTERVAL_SECONDS):
            if self.status != BrowserSessionState.READY:
                return
            try:
                process = self._failed_required_process()
            except Exception:
                continue
            if process is None:
                continue
            with suppress(BrowserSessionNotFoundError):
                self._info = self._state.update_browser_session(
                    self.session_id,
                    status=BrowserSessionState.ERROR,
                )
            self._emit_event(
                {
                    "type": "computer.error",
                    "computer_id": self.computer_id,
                    "sandbox_id": self.sandbox_id,
                    "process": process,
                    "message": (
                        f"Required desktop process '{process}' stopped in computer "
                        f"'{self.computer_id}'; call computer.delete() and create another."
                    ),
                }
            )
            return

    def _stop_monitor(self) -> None:
        monitor_stop = getattr(self, "_monitor_stop", None)
        if monitor_stop is None:
            return
        monitor_stop.set()
        thread = getattr(self, "_monitor_thread", None)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=12)
        self._monitor_thread = None

    def delete(self) -> None:
        """Delete every owned resource while keeping failed cleanup retryable."""
        if self._vm is None:
            raise SmolVMError(
                f"Computer '{self.session_id}' cannot be deleted because it is unavailable; "
                f"run 'smolvm computer delete {self.session_id}' to try again."
            )
        self._stop_monitor()
        with suppress(BrowserSessionNotFoundError):
            self._info = self._state.update_browser_session(
                self.session_id,
                status=BrowserSessionState.STOPPING,
            )
        self._emit_event(
            {
                "type": "computer.stopping",
                "computer_id": self.computer_id,
                "sandbox_id": self.sandbox_id,
            }
        )
        if self._vm.status == VMState.RUNNING:
            with suppress(Exception):
                self._vm.run("/usr/local/bin/smolvm-browser-session stop", timeout=30)
            with suppress(Exception):
                self.collect_artifacts()
        try:
            self._vm.delete()
        except Exception:
            with suppress(BrowserSessionNotFoundError):
                self._info = self._state.update_browser_session(
                    self.session_id,
                    status=BrowserSessionState.ERROR,
                )
            raise
        with suppress(BrowserSessionNotFoundError):
            self._state.delete_browser_session(self.session_id)
        self._info = self._info.model_copy(update={"status": BrowserSessionState.DELETED})
        self._emit_event(
            {
                "type": "computer.deleted",
                "computer_id": self.computer_id,
                "sandbox_id": self.sandbox_id,
            }
        )
        self.close()

    def close(self) -> None:
        """Stop health checks and release local resources."""
        self._stop_monitor()
        super().close()

    def stop(self) -> _ComputerSandbox:
        """Compatibility hook used by the inherited context manager."""
        self.delete()
        return self

    def __exit__(self, *args: object) -> None:
        """Delete on context exit and surface cleanup failures."""
        if self._owns_session:
            self.delete()
        else:
            self.close()
