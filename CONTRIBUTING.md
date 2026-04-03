# Contributing to SmolVM

Thanks for your interest in contributing to SmolVM.

SmolVM is developed in the open at [CelestoAI/SmolVM](https://github.com/CelestoAI/SmolVM), and we welcome bug reports, fixes, tests, docs improvements, and feature work.

## Security First

If you think you found a security vulnerability, do not open a public issue.

Use GitHub private vulnerability reporting:

- https://github.com/CelestoAI/SmolVM/security/advisories/new

For policy details, see [SECURITY.md](SECURITY.md).

## Ways to Contribute

- Report bugs with clear repro steps and environment details.
- Propose features through GitHub issues before large implementation work.
- Improve docs and examples.
- Submit code changes with tests.

## Development Setup

1. Fork and clone the repository.
2. Create a feature branch from `main`.
3. Install dependencies with `uv`.

```bash
uv sync --extra dev
```

Optional dashboard dependencies:

```bash
uv sync --extra dev --extra dashboard
```

### Runtime Prerequisites (for backend/runtime work)

```bash
uv run smolvm setup
```

The repo-level shell scripts in `scripts/` remain the implementation detail behind `smolvm setup`; use them directly only if you are intentionally working on the setup flow itself.

Health check:

```bash
uv run smolvm doctor
uv run smolvm doctor --json --strict
```

## Quality Checks

Run these before opening a PR:

```bash
uv run pytest
uv run ruff check .
uv run ruff format .
uv run mypy src
```

Optional pre-commit setup:

```bash
uv run pre-commit install
uv run pre-commit run --all-files
```

Notes:

- Type annotations are expected for new code (`mypy` runs in strict mode).
- Keep tests deterministic and avoid requiring privileged host setup unless the test explicitly targets runtime integration paths.

## Pull Request Guidelines

- Keep PRs focused and small enough to review.
- Include a clear description of what changed and why.
- Link related issues (for example: `Fixes #123`).
- Add or update tests for behavior changes.
- Update `README.md` or other docs when user-visible behavior changes.
- Ensure CI is green before requesting final review.

## Commit Style

- Use clear, imperative commit messages.
- Avoid mixing unrelated refactors with functional changes.

## Review and Merge

- Maintainers review PRs on a best-effort basis.
- A maintainer will merge after review and passing checks.
- Releases are handled by maintainers (tag-driven publish workflow).
