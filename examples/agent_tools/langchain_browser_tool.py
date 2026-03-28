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

"""Use LangChain with a disposable SmolVM browser session.

Install:
    pip install smolvm playwright langchain langchain-openai

Required environment:
    export OPENAI_API_KEY=...

Optional environment:
    export LANGCHAIN_MODEL=openai:gpt-4.1
    export SMOLVM_BROWSER_MODE=live

Before running:
    smolvm doctor

Example:
    python examples/agent_tools/langchain_browser_tool.py
"""

from __future__ import annotations

import os
import time
from pprint import pprint
from typing import Any

from langchain.agents import create_agent
from langchain.tools import tool

from smolvm import BrowserSession, BrowserSessionConfig

DEFAULT_MODEL = "openai:gpt-5.4"
DEFAULT_HOME_URL = "https://celesto.ai"


def _connect_with_retry(
    session: BrowserSession, attempts: int = 5, delay_seconds: float = 1.0
) -> Any:
    """Retry the initial CDP handshake while the guest browser finishes booting."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return session.connect_playwright()
        except Exception as error:
            last_error = error
            if attempt == attempts - 1:
                break
            time.sleep(delay_seconds)
    assert last_error is not None
    raise last_error


@tool
def copy_first_celesto_blog_headline(home_url: str = DEFAULT_HOME_URL) -> str:
    """Open Celesto AI in SmolVM, click Blog, and return the first blog headline."""
    with BrowserSession(
        BrowserSessionConfig(
            mode=os.environ.get("SMOLVM_BROWSER_MODE", "live"),
            viewport={"width": 1440, "height": 900},
        )
    ) as session:
        browser = _connect_with_retry(session)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()

        page.goto(home_url, wait_until="domcontentloaded")
        with context.expect_page() as blog_page_info:
            page.locator('a[href="https://celesto.ai/blog"]').first.click()
        blog_page = blog_page_info.value
        blog_page.wait_for_load_state("domcontentloaded")

        headline_locator = blog_page.locator("main").locator('a[href*="/blog/posts/"]').first
        headline_locator.wait_for()
        headline = headline_locator.inner_text().strip()

        lines = [
            f"session_id: {session.session_id}",
            f"page_url: {blog_page.url}",
            f"headline: {headline}",
            f"cdp_url: {session.cdp_url}",
        ]
        if session.live_url:
            lines.append(f"live_url: {session.live_url}")
        if session.artifacts_dir:
            lines.append(f"artifacts_dir: {session.artifacts_dir}")
        return "\n".join(lines)


def main() -> None:
    """Run a LangChain agent that extracts the latest Celesto blog headline."""
    agent = create_agent(
        model=os.environ.get("LANGCHAIN_MODEL", DEFAULT_MODEL),
        tools=[copy_first_celesto_blog_headline],
        system_prompt=(
            "You can inspect websites through a disposable SmolVM browser session. "
            "When the user asks for the first Celesto AI blog headline, call "
            "copy_first_celesto_blog_headline exactly once and answer with the "
            "headline plus any useful session details."
        ),
    )
    prompt = (
        "Visit https://celesto.ai, click the Blog button in the navigation bar, "
        "and copy the headline of the first blog post."
    )
    result = agent.invoke({"messages": [{"role": "user", "content": prompt}]})
    pprint(result)


if __name__ == "__main__":
    main()
