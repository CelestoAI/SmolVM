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

"""Run an autonomous browser task with SmolVM and OpenAI computer use.

Install:
    pip install smolvm openai playwright

Required environment:
    export OPENAI_API_KEY=...

Optional environment:
    export COMPUTER_USE_MODEL=gpt-5.4
    export SMOLVM_BROWSER_MODE=live

Before running:
    smolvm doctor

Examples:
    python examples/agent_tools/computer_use_browser.py
    python examples/agent_tools/computer_use_browser.py \
        --start-url https://example.com \
        --task "Open the page and summarize the main heading."
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

from smolvm import SmolVM, SmolVMError

if TYPE_CHECKING:
    from openai.types.responses import ResponseComputerToolCall
    from playwright.sync_api import Browser, BrowserContext, Page


DEFAULT_MODEL = "gpt-5.4"
DEFAULT_START_URL = "https://celesto.ai"
DEFAULT_TASK = (
    "Visit the site, find where its blog posts are listed, and return the headline "
    "of the first blog post. The Blog link may open a new page, a new tab, or a "
    "blog section on the current site."
)
DEFAULT_MAX_STEPS = 20

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
class ComputerUseConfig:
    task: str
    start_url: str
    allowed_domains: tuple[str, ...]
    browser_mode: Literal["headless", "live"]
    viewport_width: int
    viewport_height: int
    max_steps: int
    model: str


@dataclass(frozen=True)
class ComputerUseResult:
    final_answer: str
    session_id: str
    page_url: str
    cdp_url: str | None
    viewer_url: str | None
    display_url: str | None
    artifacts_dir: str | None


def _log(message: str) -> None:
    print(f"[smolvm-computer-use] {message}", file=sys.stderr, flush=True)


def _normalized_host(url: str) -> str:
    return (urlparse(url).hostname or "").strip().lower()


def _build_config(args: argparse.Namespace) -> ComputerUseConfig:
    mode = (args.mode or os.environ.get("SMOLVM_BROWSER_MODE", "live")).strip().lower()
    browser_mode: Literal["headless", "live"] = "headless" if mode == "headless" else "live"
    domains: list[str] = []
    start_host = _normalized_host(args.start_url)
    if start_host:
        domains.append(start_host)
    for domain in args.allowed_domain:
        normalized = domain.strip().lower()
        if normalized and normalized not in domains:
            domains.append(normalized)

    return ComputerUseConfig(
        task=args.task,
        start_url=args.start_url,
        allowed_domains=tuple(domains),
        browser_mode=browser_mode,
        viewport_width=1440,
        viewport_height=900,
        max_steps=args.max_steps,
        model=os.environ.get("COMPUTER_USE_MODEL", DEFAULT_MODEL),
    )


def _format_result(result: ComputerUseResult) -> str:
    lines = [
        f"answer: {result.final_answer}",
        f"page_url: {result.page_url}",
        f"session_id: {result.session_id}",
    ]
    if result.cdp_url:
        lines.append(f"cdp_url: {result.cdp_url}")
    if result.viewer_url:
        lines.append(f"viewer_url: {result.viewer_url}")
    if result.display_url:
        lines.append(f"display_url: {result.display_url}")
    if result.artifacts_dir:
        lines.append(f"artifacts_dir: {result.artifacts_dir}")
    return "\n".join(lines)


def _task_prompt(config: ComputerUseConfig) -> str:
    allowed_domains = ", ".join(config.allowed_domains)
    return (
        f"Start from {config.start_url}.\n"
        f"Task: {config.task}\n"
        f"Allowed domains: {allowed_domains}\n"
        "Use the computer tool for all browser interaction.\n"
        "Base your actions on what is visible on screen.\n"
        "Ignore unrelated media, ads, and external links.\n"
        "Do not leave the allowed domains.\n"
        "If a click keeps you on the same page, check whether it opened a new tab "
        "or scrolled to the relevant section before giving up.\n"
        "If the task requires login, payment, destructive changes, "
        "or another domain, stop and explain why.\n"
        "Do not stop until you either find the requested text or exhaust "
        "the obvious on-site navigation.\n"
        "Return only the requested answer once you have it."
    )


def _is_allowed_url(url: str, allowed_domains: tuple[str, ...]) -> bool:
    parsed = urlparse(url)
    if parsed.scheme in {"about", "blob", "data"}:
        return True
    if parsed.scheme in {"http", "https"}:
        return _normalized_host(url) in allowed_domains
    return url == "" or parsed.scheme == ""


def _require_dependency(import_path: str, install_hint: str) -> Any:
    module_name, _, attr_name = import_path.partition(":")
    try:
        module = __import__(module_name, fromlist=[attr_name] if attr_name else [])
    except ImportError as exc:
        raise RuntimeError(
            f"Missing dependency '{module_name}'. Install it with: {install_hint}"
        ) from exc
    return getattr(module, attr_name) if attr_name else module


def _active_page(context: BrowserContext, page: Page | None) -> Page:
    if page is not None and not page.is_closed():
        return page
    for candidate in reversed(context.pages):
        if not candidate.is_closed():
            return candidate
    raise RuntimeError("The browser context does not have an open page.")


def _capture_data_url(page: Page) -> str:
    png_bytes = page.screenshot(type="png", full_page=False)
    return f"data:image/png;base64,{base64.b64encode(png_bytes).decode('ascii')}"


def _extract_text(response: Any) -> str:
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


def _first_computer_call(response: Any) -> ResponseComputerToolCall | None:
    for item in getattr(response, "output", []):
        if getattr(item, "type", None) == "computer_call":
            return item
    return None


def _normalize_key(key: str) -> str:
    normalized = key.strip()
    if not normalized:
        return normalized
    upper = normalized.upper()
    if upper in _KEY_MAP:
        return _KEY_MAP[upper]
    if len(normalized) == 1:
        return normalized
    return normalized


def _hold_keys(page: Page, keys: list[str] | None) -> list[str]:
    normalized = [_normalize_key(key) for key in keys or [] if key.strip()]
    for key in normalized:
        page.keyboard.down(key)
    return normalized


def _release_keys(page: Page, keys: list[str]) -> None:
    for key in reversed(keys):
        with suppress(Exception):
            page.keyboard.up(key)


def _click_button(button: str) -> str:
    if button == "wheel":
        return "middle"
    return "left" if button in {"back", "forward"} else button


def _format_keys(keys: list[str] | None) -> str:
    normalized = [_normalize_key(key) for key in keys or [] if key.strip()]
    return "+".join(normalized)


def _describe_action(action: Any) -> str:
    action_type = getattr(action, "type", "unknown")
    keys = _format_keys(getattr(action, "keys", None))
    keys_prefix = f"{keys}+" if keys else ""

    if action_type == "click":
        button = getattr(action, "button", "left")
        return f"{keys_prefix}click {button} @ ({action.x}, {action.y})"
    if action_type == "double_click":
        return f"{keys_prefix}double_click @ ({action.x}, {action.y})"
    if action_type == "move":
        return f"{keys_prefix}move -> ({action.x}, {action.y})"
    if action_type == "scroll":
        return (
            f"{keys_prefix}scroll @ ({action.x}, {action.y}) "
            f"delta=({action.scroll_x}, {action.scroll_y})"
        )
    if action_type == "keypress":
        return f"keypress {_format_keys(getattr(action, 'keys', None)) or '<none>'}"
    if action_type == "type":
        text = getattr(action, "text", "")
        preview = text if len(text) <= 40 else f"{text[:37]}..."
        return f"type {preview!r}"
    if action_type == "drag":
        path = list(getattr(action, "path", []) or [])
        if not path:
            return "drag <empty path>"
        start = path[0]
        end = path[-1]
        return f"{keys_prefix}drag ({start.x}, {start.y}) -> ({end.x}, {end.y})"
    if action_type == "wait":
        return "wait"
    if action_type == "screenshot":
        return "screenshot"
    return action_type


def _run_action(page: Page, action: Any) -> None:
    action_type = getattr(action, "type", None)
    held_keys = _hold_keys(page, getattr(action, "keys", None))
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
            page.mouse.click(action.x, action.y, button=_click_button(button))
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
                page.keyboard.press(_normalize_key(key))
            return

        if action_type == "type":
            page.keyboard.type(action.text)
            return

        if action_type == "drag":
            path = list(getattr(action, "path", []) or [])
            if not path:
                return
            start = path[0]
            page.mouse.move(start.x, start.y)
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
        _release_keys(page, held_keys)


def _run_actions(page: Page, actions: list[Any]) -> None:
    for action in actions:
        _run_action(page, action)
    page.wait_for_timeout(400)
    with suppress(Exception):
        page.wait_for_load_state("domcontentloaded", timeout=2000)


def _enforce_allowed_pages(
    context: BrowserContext,
    current_page: Page | None,
    config: ComputerUseConfig,
) -> Page:
    allowed_pages: list[Page] = []
    blocked_urls: list[str] = []
    for candidate in list(context.pages):
        if candidate.is_closed():
            continue
        if _is_allowed_url(candidate.url, config.allowed_domains):
            allowed_pages.append(candidate)
            continue
        blocked_urls.append(candidate.url)
        with suppress(Exception):
            candidate.close()

    if blocked_urls:
        _log("closed blocked page(s): " + ", ".join(blocked_urls))

    if allowed_pages:
        selected = _active_page(context, current_page if current_page in allowed_pages else None)
        with suppress(Exception):
            selected.bring_to_front()
        return selected

    recovered_page = context.new_page()
    recovered_page.goto(config.start_url, wait_until="domcontentloaded")
    _log(f"reopened allowed start URL: {config.start_url}")
    return recovered_page


def _check_safety(computer_call: ResponseComputerToolCall) -> None:
    checks = getattr(computer_call, "pending_safety_checks", []) or []
    if not checks:
        return
    messages = [check.message for check in checks if getattr(check, "message", None)]
    _log("model requested human review: " + "; ".join(messages or ["pending safety checks"]))
    raise RuntimeError(
        "The model requested a guarded action that needs human review: "
        + "; ".join(messages or ["pending safety checks"])
    )


def _run_task(config: ComputerUseConfig) -> ComputerUseResult:
    openai_client_cls = _require_dependency("openai:OpenAI", "pip install openai")
    client = openai_client_cls()

    with SmolVM.browser(
        headless=config.browser_mode == "headless",
        viewport={"width": config.viewport_width, "height": config.viewport_height},
    ) as session:
        _log(
            f"browser sandbox started id={session.session_id} mode={config.browser_mode} "
            f"start_url={config.start_url}"
        )
        if session.cdp_url is None:
            raise SmolVMError(
                "The browser sandbox failed to provide a connection address needed to "
                "control the browser; please try again or report this problem."
            )
        _log(f"cdp_url={session.cdp_url}")
        if session.viewer_url:
            _log(f"viewer_url={session.viewer_url}")
        if session.display_url:
            _log(f"display_url={session.display_url}")

        browser: Browser = session.connect_playwright()
        context = browser.contexts[0] if browser.contexts else browser.new_context(
            viewport={"width": config.viewport_width, "height": config.viewport_height}
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(config.start_url, wait_until="domcontentloaded")
        page = _enforce_allowed_pages(context, page, config)
        _log(f"loaded {page.url}")

        response = client.responses.create(
            model=config.model,
            tools=[{"type": "computer"}],
            input=_task_prompt(config),
        )
        _log(f"submitted task to model={config.model}")

        for step_number in range(1, config.max_steps + 1):
            _log(f"step {step_number}/{config.max_steps}")
            computer_call = _first_computer_call(response)
            if computer_call is None:
                final_answer = _extract_text(response)
                if not final_answer:
                    raise RuntimeError("The model stopped without returning a final answer.")
                page = _enforce_allowed_pages(context, page, config)
                _log(f"final answer: {final_answer}")
                return ComputerUseResult(
                    final_answer=final_answer,
                    session_id=session.session_id,
                    page_url=page.url,
                    cdp_url=session.cdp_url,
                    viewer_url=session.viewer_url,
                    display_url=session.display_url,
                    artifacts_dir=str(session.artifacts_dir) if session.artifacts_dir else None,
                )

            _check_safety(computer_call)
            actions = list(getattr(computer_call, "actions", []) or [])
            if actions:
                _log("actions: " + " | ".join(_describe_action(action) for action in actions))
            else:
                _log("actions: <none>")
            previous_url = page.url
            _run_actions(page, actions)
            page = _enforce_allowed_pages(context, _active_page(context, page), config)
            if page.url != previous_url:
                _log(f"url changed: {previous_url} -> {page.url}")
            else:
                _log(f"url unchanged: {page.url}")
            screenshot_data_url = _capture_data_url(page)

            response = client.responses.create(
                model=config.model,
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

        raise RuntimeError(
            f"The computer-use loop exceeded max_steps={config.max_steps} before finishing."
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an autonomous browser task in a disposable SmolVM session."
    )
    parser.add_argument("--task", default=DEFAULT_TASK, help="Natural-language task to perform.")
    parser.add_argument(
        "--start-url",
        default=DEFAULT_START_URL,
        help="Initial URL to open before the model starts navigating.",
    )
    parser.add_argument(
        "--allowed-domain",
        action="append",
        default=[],
        help="Additional allowed domain. Repeat this flag to allow more than one domain.",
    )
    parser.add_argument(
        "--mode",
        choices=("headless", "live"),
        default=None,
        help="Browser mode. Defaults to SMOLVM_BROWSER_MODE or live.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=DEFAULT_MAX_STEPS,
        help="Maximum number of computer-use turns before stopping.",
    )
    return parser


def main() -> int:
    """Run the OpenAI computer-use example."""
    config = _build_config(_build_parser().parse_args())
    result = _run_task(config)
    print(_format_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
