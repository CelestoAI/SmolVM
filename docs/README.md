# SmolVM documentation

SmolVM gives an AI agent a disposable computer for running code, using a browser, and doing work without changing your machine. Start with installation, then choose the workflow you need.

## Get started

- [Install SmolVM](installation.md) — prepare your machine and check it is ready.
- [Run a sandbox](guides/sandboxes.md) — create, use, and remove an isolated computer.
- [CLI reference](reference/cli.md) — scan every current command and its purpose.

## Guides

- [Agent presets](guides/agent-presets.md) — start Codex, Claude Code, Pi, Hermes, or OpenClaw in a sandbox.
- [Browser sandboxes](guides/browser.md) — run Chromium and connect with a browser or Playwright.
- [Snapshots](guides/snapshots.md) — save and restore supported sandbox state.
- [Networking](guides/networking.md) — share a local port, limit outbound domains, or connect a sandbox to an existing bridge.
- [macOS desktops](guides/macos.md) — open a disposable Mac desktop on Apple Silicon.
- [Windows guests](guides/windows.md) — build and use a Windows image.

## Contributors

- [Architecture](contributing/architecture.md) — find the part of the codebase that owns a behavior.
- [macOS runtime spike](contributing/macos-spike.md) — see verified behavior and release blockers for the desktop preview.
- [Native core](contributing/native-core.md) — work on the optional Rust acceleration package.

Pages link to the implementation and relevant tests when they make a claim about current behavior. Those links are evidence for maintainers, not prerequisites for using SmolVM.
