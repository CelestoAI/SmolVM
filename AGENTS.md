# SmolVM Context

SmolVM gives AI agents their own disposable computer. Each sandbox is a lightweight virtual machine that boots in seconds, runs any code or command you throw at it, and disappears when you're done — nothing touches the host.

## 🚀 Project Overview

SmolVM is specifically designed to provide a secure "sandbox" for AI agents to execute code, browse the web, or perform system-level tasks safely.


## 🧪 Development

### Key Commands
- **Testing:** `pytest` (runs the suite in `tests/`)
- **Linting & Formatting:** `uv run ruff check .` or `uv run ruff format .`

### CLI design

- New CLI commands follow a **NOUN-VERB** structure: `smolvm <noun> <verb>`,
  e.g. `smolvm codex start`, not `smolvm start codex`.
- The noun names the resource (a sandbox, a harness, a browser session); the
  verb names the action on it (`start`, `stop`, `ssh`).
- This scales naturally as actions grow: `smolvm codex start`, then later
  `smolvm codex logs`, `smolvm codex status`, etc.
- When adding a new harness or resource, register it as a top-level
  subcommand and put its actions underneath, instead of overloading a
  global verb.

### Core writing principles
- Follow progressive disclosure of complexity.
- Lead with outcomes, not implementation details.
- The first paragraph of every page must be plain English with no jargon.
- Assume the reader may be a beginner engineer or even a non-developer.
- Do not assume prior knowledge.
- Explain what the user can do and why it matters before explaining how it works.
- Do not introduce a new concept unless the page truly needs it.
- If you must use a technical term, explain it immediately in simple language.
- Prefer short, concrete sentences over dense explanations.
