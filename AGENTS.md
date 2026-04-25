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

### User-facing errors and warnings

Error and warning messages are UX, not stack traces. The reader may
be a first-time user with no idea how SmolVM works internally — they
must still be able to act on the message. Every user-facing message
(CLI output, panels, JSON `error` payloads, JSON `warnings` entries)
should:

- **State the fact in plain English.** Avoid internal vocabulary
  ("mount", "host", "tap device", "validator") even when those words
  appear in flag names — the user did not necessarily set the flag.
- **Name the recovery.** Include the exact recovery command, with the
  actual sandbox name interpolated, not a placeholder.
- **Stay short.** One sentence is the goal; two if you must. If you
  reach for a third sentence, you are probably explaining a
  consequence that is either false in some state or not actionable —
  cut it.
- **Skip consequences you cannot guarantee.** A warning that says
  "the sandbox cannot start" is wrong if the sandbox is currently
  running. Saying "won't be able to restart once stopped" is true but
  irrelevant when the user may not plan to restart anyway. Prefer
  phrasing that is true regardless of state and let the user judge
  the impact.

The same rule applies to JSON consumers — agents benefit from the
same self-contained context. Don't split the message across the human
output and a separate hint that JSON callers will not see.

**Bad** — internal vocabulary, no recovery path:

```
workspace mount missing on host: /Users/aniket/conductor/workspaces/SmolVM/lome
```

**Bad** — plain language, but too long and makes a state-dependent
claim ("cannot start") that is false for a sandbox the user can SSH
into right now:

```
This sandbox was set up to share the folder '...' with you, but that
folder no longer exists on your machine. The sandbox cannot start
until you put the folder back, or delete the sandbox with
'smolvm delete sbx-einstein'.
```

**Good** — one sentence, true in every state, names the recovery:

```
Shared folder is missing on your machine:
'/Users/aniket/conductor/workspaces/SmolVM/lome'. Restore it, or run
'smolvm delete sbx-einstein' to remove the sandbox.
```
