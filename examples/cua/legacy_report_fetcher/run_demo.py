#!/usr/bin/env python3

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

"""Demo: use a SmolVM computer-use agent to fetch legacy reports."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse

from smolvm import BrowserSession, BrowserSessionConfig, CommandResult, SmolVM, WorkspaceMount

DEMO_DIR = Path(__file__).resolve().parent
GUEST_ROOT = "/workspace/legacy_report_fetcher"
PORTAL_URL = "http://127.0.0.1:8000"
DEFAULT_MODEL = "gpt-5.4"
DEFAULT_MAX_STEPS = 24

T = TypeVar("T")

_KEY_MAP = {
    "ALT": "Alt",
    "BACKSPACE": "Backspace",
    "CMD": "Meta",
    "COMMAND": "Meta",
    "CTRL": "Control",
    "CONTROL": "Control",
    "DELETE": "Delete",
    "DOWN": "ArrowDown",
    "END": "End",
    "ENTER": "Enter",
    "ESC": "Escape",
    "ESCAPE": "Escape",
    "HOME": "Home",
    "LEFT": "ArrowLeft",
    "OPTION": "Alt",
    "PAGEDOWN": "PageDown",
    "PAGEUP": "PageUp",
    "RETURN": "Enter",
    "RIGHT": "ArrowRight",
    "SHIFT": "Shift",
    "SPACE": " ",
    "TAB": "Tab",
    "UP": "ArrowUp",
}


@dataclass(frozen=True)
class AgentResult:
    """Final result from the computer-use loop."""

    final_answer: str
    steps: int


def log(message: str) -> None:
    """Print progress to stderr so stdout can stay demo-friendly."""
    print(f"[legacy-report-demo] {message}", file=sys.stderr, flush=True)


def run_with_heartbeat(
    label: str,
    func: Callable[[], T],
    *,
    interval_seconds: float = 10.0,
) -> T:
    """Run a blocking step and log periodically so the demo never looks stuck."""
    started = time.monotonic()
    stop = threading.Event()

    def heartbeat() -> None:
        while not stop.wait(interval_seconds):
            elapsed = int(time.monotonic() - started)
            log(f"{label} still running ({elapsed}s elapsed)")

    log(f"{label}...")
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        result = func()
    except Exception:
        elapsed = int(time.monotonic() - started)
        log(f"{label} failed after {elapsed}s")
        raise
    finally:
        stop.set()
        thread.join(timeout=0.2)

    elapsed = int(time.monotonic() - started)
    log(f"{label} complete ({elapsed}s)")
    return result


def vm_exec(vm: SmolVM, *args: str, timeout: int = 60) -> str:
    """Run a short shell command inside the SmolVM sandbox."""
    result: CommandResult = vm.run(shlex.join(args), timeout=timeout)
    if not result.ok:
        raise RuntimeError(result.stderr.strip() or result.stdout)
    return result.stdout


def start_legacy_app(vm: SmolVM) -> None:
    """Start the mounted fake legacy app inside the sandbox."""
    output = vm_exec(
        vm,
        "python3",
        f"{GUEST_ROOT}/ops/start_portal.py",
        "--root",
        GUEST_ROOT,
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        timeout=30,
    )
    log(output.strip())


def expected_download_filenames(report_date: str) -> list[str]:
    """Return the report filenames the demo expects Chromium to create."""
    return [f"orders_{report_date}.csv", f"inventory_{report_date}.csv"]


def downloads_ready(vm: SmolVM, session_id: str, report_date: str) -> bool:
    """Return True when both expected downloads exist in the sandbox."""
    download_dir = guest_download_dir(session_id)
    expected = expected_download_filenames(report_date)
    script = (
        "from pathlib import Path; "
        f"p=Path({download_dir!r}); "
        f"expected={expected!r}; "
        "missing=[name for name in expected if not (p/name).exists()]; "
        "partial=list(p.glob('*.crdownload')); "
        "raise SystemExit(1 if missing or partial else 0)"
    )
    result = vm.run(shlex.join(["python3", "-c", script]), timeout=20)
    return result.ok


def wait_until_downloads_ready(
    vm: SmolVM,
    session_id: str,
    report_date: str,
    *,
    timeout: float = 20.0,
) -> None:
    """Wait until both report files exist in the sandbox download folder."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if downloads_ready(vm, session_id, report_date):
            return
        time.sleep(0.5)
    raise RuntimeError(
        f"Timed out waiting for report downloads for session {session_id}. "
        f"Rerun: uv run --with openai --with playwright "
        f"examples/cua/legacy_report_fetcher/run_demo.py --mode live "
        f"--report-date {report_date}\n"
        f"{list_downloads(vm, session_id).strip()}"
    )


def ensure_report_downloads(
    page: Any,
    vm: SmolVM,
    session_id: str,
    report_date: str,
) -> None:
    """Verify downloads, then recover with direct CDP clicks if the agent stopped early."""
    if downloads_ready(vm, session_id, report_date):
        return

    log(
        "Agent stopped before the CSV files appeared in the sandbox; "
        "using CDP to click the report download links."
    )
    page.goto(f"{PORTAL_URL}/reports", wait_until="domcontentloaded")
    with suppress(Exception):
        page.fill("#report-date", report_date)
    page.click("#generate")
    page.wait_for_selector("#orders-link", timeout=5000)
    page.click("#orders-link")
    page.wait_for_selector("#inventory-link", timeout=5000)
    page.click("#inventory-link")
    try:
        wait_until_downloads_ready(vm, session_id, report_date, timeout=8.0)
        return
    except RuntimeError:
        log(
            "CDP clicks still did not create files in the sandbox download folder; "
            "using the sandbox shell to fetch the generated CSV files."
        )

    download_reports_with_vm_shell(vm, session_id, report_date)
    wait_until_downloads_ready(vm, session_id, report_date)


def download_reports_with_vm_shell(vm: SmolVM, session_id: str, report_date: str) -> None:
    """Fetch report files from the local portal using the sandbox shell."""
    download_dir = guest_download_dir(session_id)
    script = "\n".join(
        [
            "from pathlib import Path",
            "from urllib.parse import urlencode",
            "from urllib.request import Request, urlopen",
            f"download_dir = Path({download_dir!r})",
            "download_dir.mkdir(parents=True, exist_ok=True)",
            f"report_date = {report_date!r}",
            "base_url = 'http://127.0.0.1:8000/download/'",
            "for name in ('orders', 'inventory'):",
            "    query = urlencode({'date': report_date})",
            "    url = base_url + name + '?' + query",
            "    request = Request(url, headers={'Cookie': 'acme_session=demo'})",
            "    with urlopen(request, timeout=10) as response:",
            "        data = response.read()",
            "    target = download_dir / f'{name}_{report_date}.csv'",
            "    target.write_bytes(data)",
            "    print(target)",
        ]
    )
    vm_exec(vm, "python3", "-c", script, timeout=30)


def copy_file_from_vm(vm: SmolVM, guest_path: str, host_path: Path) -> Path:
    """Copy one file from the sandbox to the host."""
    log(f"Copying {guest_path} to {host_path}")
    return Path(vm.download_file(guest_path, host_path))


def sha256_file(path: Path) -> str:
    """Return a SHA-256 digest for a local file."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_reports_to_host(vm: SmolVM, session_id: str, report_date: str) -> Path:
    """List sandbox downloads, then copy reports to the host handoff folder."""
    log(list_downloads(vm, session_id).strip())
    inbox = DEMO_DIR / "artifacts" / "inbox" / "acme" / report_date
    inbox.mkdir(parents=True, exist_ok=True)

    files: list[dict[str, str | int]] = []
    for filename in expected_download_filenames(report_date):
        guest_path = f"{guest_download_dir(session_id)}/{filename}"
        local_path = copy_file_from_vm(vm, guest_path, inbox / filename)
        files.append(
            {
                "name": filename,
                "path": str(local_path),
                "size_bytes": local_path.stat().st_size,
                "sha256": sha256_file(local_path),
                "status": "downloaded",
            }
        )

    manifest = {
        "source": "acme_legacy_reports",
        "run_date": date.today().isoformat(),
        "report_date": report_date,
        "status": "success",
        "files": files,
    }
    manifest_path = inbox / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return inbox


def run_pipeline(inbox: Path) -> str:
    """Run the existing-pipeline stand-in on the host handoff folder."""
    result = subprocess.run(
        [sys.executable, str(DEMO_DIR / "pipeline" / "import_reports.py"), str(inbox)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout)
    return result.stdout


def guest_download_dir(session_id: str) -> str:
    """Return the browser download folder inside the sandbox."""
    return f"/opt/smolvm-browser/downloads/{session_id}"


def configure_browser_downloads(browser: Any, download_dir: str) -> None:
    """Tell Chromium to save clicked downloads into the sandbox download folder."""
    cdp = browser.new_browser_cdp_session()
    cdp.send(
        "Browser.setDownloadBehavior",
        {
            "behavior": "allow",
            "downloadPath": download_dir,
            "eventsEnabled": True,
        },
    )


def list_downloads(vm: SmolVM, session_id: str) -> str:
    """List current browser downloads for debugging and validation."""
    return vm_exec(
        vm,
        "python3",
        "-c",
        (
            "from pathlib import Path; "
            f"p=Path({guest_download_dir(session_id)!r}); "
            "print('download_dir:', p); "
            "print('files:', ', '.join(sorted(x.name for x in p.glob('*'))) or '<empty>')"
        ),
        timeout=20,
    )


def normalize_key(key: str) -> str:
    """Translate computer-use key names to Playwright key names."""
    normalized = key.strip()
    if not normalized:
        return normalized
    upper = normalized.upper()
    if upper in _KEY_MAP:
        return _KEY_MAP[upper]
    if len(normalized) == 1:
        return normalized
    return normalized


def hold_keys(page: Any, keys: list[str] | None) -> list[str]:
    """Hold modifier keys before a mouse action."""
    normalized = [normalize_key(key) for key in keys or [] if key.strip()]
    for key in normalized:
        page.keyboard.down(key)
    return normalized


def release_keys(page: Any, keys: list[str]) -> None:
    """Release modifier keys after a mouse action."""
    for key in reversed(keys):
        with suppress(Exception):
            page.keyboard.up(key)


def click_button(button: str) -> str:
    """Map model button labels to Playwright button labels."""
    if button == "wheel":
        return "middle"
    return "left" if button in {"back", "forward"} else button


def run_action(page: Any, action: Any) -> None:
    """Apply one OpenAI computer-use action through Playwright/CDP."""
    action_type = getattr(action, "type", None)
    held_keys = hold_keys(page, getattr(action, "keys", None))
    try:
        if action_type == "click":
            button = getattr(action, "button", "left")
            if button == "back":
                with suppress(Exception):
                    page.go_back(wait_until="domcontentloaded")
                return
            if button == "forward":
                with suppress(Exception):
                    page.go_forward(wait_until="domcontentloaded")
                return
            page.mouse.click(action.x, action.y, button=click_button(button))
            return
        if action_type == "double_click":
            page.mouse.dblclick(action.x, action.y)
            return
        if action_type == "move":
            page.mouse.move(action.x, action.y)
            return
        if action_type == "scroll":
            page.mouse.move(action.x, action.y)
            page.mouse.wheel(action.scroll_x, action.scroll_y)
            return
        if action_type == "keypress":
            for key in getattr(action, "keys", []) or []:
                page.keyboard.press(normalize_key(key))
            return
        if action_type == "type":
            page.keyboard.type(action.text)
            return
        if action_type == "drag":
            path = list(getattr(action, "path", []) or [])
            if not path:
                return
            page.mouse.move(path[0].x, path[0].y)
            page.mouse.down()
            for point in path[1:]:
                page.mouse.move(point.x, point.y, steps=8)
            page.mouse.up()
            return
        if action_type == "wait":
            page.wait_for_timeout(1500)
            return
        if action_type == "screenshot":
            return
        raise RuntimeError(f"Unsupported computer action: {action_type}")
    finally:
        release_keys(page, held_keys)


def describe_action(action: Any) -> str:
    """Return a compact description for demo logs."""
    action_type = getattr(action, "type", "unknown")
    if action_type in {"click", "double_click", "move", "scroll"}:
        x = getattr(action, "x", "?")
        y = getattr(action, "y", "?")
        return f"{action_type}@({x},{y})"
    if action_type == "type":
        text = getattr(action, "text", "")
        return f"type {text[:32]!r}"
    if action_type == "keypress":
        return "keypress " + "+".join(getattr(action, "keys", []) or [])
    return str(action_type)


def run_actions(page: Any, actions: list[Any]) -> None:
    """Apply a batch of model actions, then let the page settle."""
    for action in actions:
        run_action(page, action)
    page.wait_for_timeout(500)
    with suppress(Exception):
        page.wait_for_load_state("domcontentloaded", timeout=2000)


def capture_data_url(page: Any) -> str:
    """Capture the current browser viewport for the computer-use model."""
    png_bytes = page.screenshot(type="png", full_page=False)
    encoded = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def extract_text(response: Any) -> str:
    """Extract final text from an OpenAI Responses API response."""
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    chunks: list[str] = []
    for item in getattr(response, "output", []):
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", []) or []:
            text = getattr(part, "text", None)
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())
    return "\n".join(chunks).strip()


def first_computer_call(response: Any) -> Any | None:
    """Return the first computer-use tool call, if present."""
    for item in getattr(response, "output", []):
        if getattr(item, "type", None) == "computer_call":
            return item
    return None


def ensure_local_page(context: Any, page: Any, start_url: str) -> Any:
    """Keep the agent on the local demo portal."""
    allowed_hosts = {"127.0.0.1", "localhost"}
    selected = page
    for candidate in list(context.pages):
        if candidate.is_closed():
            continue
        parsed = urlparse(candidate.url)
        if parsed.scheme in {"about", "data", "blob"} or parsed.hostname in allowed_hosts:
            selected = candidate
            continue
        log(f"closed blocked page: {candidate.url}")
        with suppress(Exception):
            candidate.close()

    if selected.is_closed():
        selected = context.new_page()
        selected.goto(start_url, wait_until="domcontentloaded")
    with suppress(Exception):
        selected.bring_to_front()
    return selected


def check_safety(computer_call: Any) -> None:
    """Stop if the model asks for human review before an action."""
    checks = getattr(computer_call, "pending_safety_checks", []) or []
    if not checks:
        return
    messages = [check.message for check in checks if getattr(check, "message", None)]
    raise RuntimeError(
        "The model requested a guarded action that needs human review: "
        + "; ".join(messages or ["pending safety checks"])
    )


def task_prompt(start_url: str, report_date: str) -> str:
    """Build the task for the computer-use agent."""
    return f"""
Start from {start_url}.

Use the browser to log into Acme Legacy Reports Portal.
Username: ops@acme.test
Password: demo-password

Download exactly these two reports for {report_date}:
- Orders CSV
- Inventory CSV

Important: clicking Generate reports is not enough. After the reports are ready,
you must click the visible "Download Orders CSV" link and the visible
"Download Inventory CSV" link.

Do not download Settlements PDF or any other file.
Stay on http://127.0.0.1:8000 or localhost only.
After both CSV files have downloaded, respond only with: done
""".strip()


def run_computer_use_agent(
    *,
    page: Any,
    start_url: str,
    report_date: str,
    model: str,
    max_steps: int,
    verify_downloads: Callable[[], bool] | None = None,
) -> AgentResult:
    """Run OpenAI computer-use against the existing CDP-connected browser page."""
    from openai import OpenAI

    client = OpenAI()
    context = page.context
    page.goto(start_url, wait_until="domcontentloaded")
    page = ensure_local_page(context, page, start_url)

    response = client.responses.create(
        model=model,
        tools=[{"type": "computer"}],
        input=task_prompt(start_url, report_date),
    )
    log(f"submitted task to model={model}")

    validation_retries = 0
    for step_number in range(1, max_steps + 1):
        computer_call = first_computer_call(response)
        if computer_call is None:
            final_answer = extract_text(response)
            if not final_answer:
                raise RuntimeError("The model stopped without returning a final answer.")
            log(f"agent final answer: {final_answer}")
            if verify_downloads is None or verify_downloads():
                return AgentResult(final_answer=final_answer, steps=step_number - 1)
            if validation_retries >= 2:
                return AgentResult(final_answer=final_answer, steps=step_number - 1)

            validation_retries += 1
            log(
                "Agent said it was done, but the sandbox download folder is still missing "
                "one or both CSV files; asking it to continue."
            )
            response = client.responses.create(
                model=model,
                tools=[{"type": "computer"}],
                previous_response_id=response.id,
                input=(
                    "The sandbox download folder is still missing one or both CSV files. "
                    "You probably generated the reports but did not click the actual "
                    "download links. Continue using the browser. Click Generate reports if "
                    "needed, then click both visible links: Download Orders CSV and "
                    "Download Inventory CSV. Do not answer done until you have clicked both."
                ),
            )
            continue

        check_safety(computer_call)
        actions = list(getattr(computer_call, "actions", []) or [])
        log(
            f"step {step_number}/{max_steps}: "
            + (" | ".join(describe_action(action) for action in actions) or "<no action>")
        )
        run_actions(page, actions)
        page = ensure_local_page(context, page, start_url)
        screenshot_data_url = capture_data_url(page)

        response = client.responses.create(
            model=model,
            tools=[{"type": "computer"}],
            previous_response_id=response.id,
            input=[
                {
                    "type": "computer_call_output",
                    "call_id": computer_call.call_id,
                    "output": {
                        "type": "computer_screenshot",
                        "image_url": screenshot_data_url,
                        "detail": "original",
                    },
                }
            ],
        )

    raise RuntimeError(f"The computer-use loop exceeded max_steps={max_steps}.")


def require_host_dependencies() -> None:
    """Fail before booting a sandbox if demo Python packages are missing."""
    missing: list[str] = []
    for module_name, package_name in (
        ("openai", "openai"),
        ("playwright", "playwright"),
    ):
        if importlib.util.find_spec(module_name) is None:
            missing.append(package_name)

    if missing:
        package_flags = " ".join(f"--with {package_name}" for package_name in missing)
        raise RuntimeError(
            "Install the demo dependencies on your computer, then rerun: "
            f"uv run {package_flags} examples/cua/legacy_report_fetcher/run_demo.py --mode live"
        )


def parse_report_date(value: str) -> str:
    """Validate a report date before it becomes part of file paths."""
    if ".." in value or "/" in value or "\\" in value or os.sep in value:
        raise argparse.ArgumentTypeError("Report date must be YYYY-MM-DD.")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Report date must be YYYY-MM-DD.") from exc


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Run the SmolVM legacy report fetcher computer-use demo."
    )
    parser.add_argument("--mode", choices=("headless", "live"), default="live")
    parser.add_argument("--model", default=os.environ.get("COMPUTER_USE_MODEL", DEFAULT_MODEL))
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    parser.add_argument(
        "--boot-timeout",
        type=float,
        default=180.0,
        help="Seconds to wait for the SmolVM browser sandbox to boot.",
    )
    parser.add_argument(
        "--report-date",
        type=parse_report_date,
        default=(date.today() - timedelta(days=1)).isoformat(),
        help="Report date to download. Defaults to yesterday.",
    )
    return parser.parse_args()


def main() -> int:
    """Run the demo."""
    args = parse_args()
    require_host_dependencies()
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("Set OPENAI_API_KEY before running the computer-use demo.")

    screenshots_dir = DEMO_DIR / "artifacts" / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    session: BrowserSession | None = None
    try:
        session = run_with_heartbeat(
            "Preparing SmolVM browser session and mounted demo folder",
            lambda: BrowserSession(
                BrowserSessionConfig(
                    mode=args.mode,
                    record_video=args.mode == "live",
                    allow_downloads=True,
                    viewport={"width": 1440, "height": 900},
                    workspace_mounts=[
                        WorkspaceMount(
                            host_path=DEMO_DIR,
                            guest_path=GUEST_ROOT,
                            writable=True,
                        )
                    ],
                )
            ),
            interval_seconds=10.0,
        )
        run_with_heartbeat(
            f"Starting SmolVM browser sandbox (timeout {int(args.boot_timeout)}s)",
            lambda: session.start(boot_timeout=args.boot_timeout, on_progress=log),
            interval_seconds=10.0,
        )
        print(f"Session: {session.session_id}")
        print(f"VM: {session.vm_id}")
        print(f"CDP URL: {session.cdp_url}")
        print(f"Live URL: {session.live_url or '<headless>'}")
        print(f"Artifacts: {session.artifacts_dir}")

        vm = session.vm
        run_with_heartbeat(
            "Starting Acme legacy portal inside the sandbox",
            lambda: start_legacy_app(vm),
            interval_seconds=5.0,
        )

        log("Connecting to Chromium over CDP")
        browser = session.connect_playwright()
        download_dir = guest_download_dir(session.session_id)
        vm_exec(vm, "mkdir", "-p", download_dir, timeout=20)
        configure_browser_downloads(browser, download_dir)
        log(f"Browser downloads will be saved in {download_dir}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        log(f"Opening portal at {PORTAL_URL}")
        page.goto(PORTAL_URL, wait_until="domcontentloaded")
        session.screenshot(screenshots_dir / "01-portal-login.png", full_page=False)

        log("Starting OpenAI computer-use browser task")
        agent_result = run_computer_use_agent(
            page=page,
            start_url=PORTAL_URL,
            report_date=args.report_date,
            model=args.model,
            max_steps=args.max_steps,
            verify_downloads=lambda: downloads_ready(vm, session.session_id, args.report_date),
        )
        ensure_report_downloads(page, vm, session.session_id, args.report_date)
        log(f"Agent final answer after file verification: {agent_result.final_answer}")
        session.screenshot(screenshots_dir / "02-after-downloads.png", full_page=False)
        inbox = run_with_heartbeat(
            "Listing sandbox downloads and copying reports to the host",
            lambda: copy_reports_to_host(vm, session.session_id, args.report_date),
            interval_seconds=5.0,
        )
        print(f"Copied reports and wrote manifest: {inbox / 'manifest.json'}")
        print("\nPipeline output:")
        pipeline_output = run_with_heartbeat(
            "Running the existing-pipeline import on the host handoff folder",
            lambda: run_pipeline(inbox),
            interval_seconds=5.0,
        )
        print(pipeline_output.strip())
        print("\nLocal handoff folder:")
        print(inbox)
    finally:
        if session is not None:
            run_with_heartbeat(
                "Stopping SmolVM browser sandbox",
                session.stop,
                interval_seconds=5.0,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
