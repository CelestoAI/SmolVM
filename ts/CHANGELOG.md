# Changelog

## 0.1.0-preview.1

- Introduces the object-oriented `SmolVM` and `Sandbox` API.
- Adds automatic local runtime startup, authenticated SDK sessions, files, cancellation, diagnostics, and typed events.
- Makes the quickstart work in default CommonJS npm projects without configuration.
- Coalesces image-download progress and reports outdated local runtimes with an exact recovery command.
- Adds a configurable 10-minute sandbox-creation deadline and retry-safe timeout cleanup.

During 0.x, patch releases are API-compatible. Breaking changes require a minor release and a copy-paste migration guide here.
