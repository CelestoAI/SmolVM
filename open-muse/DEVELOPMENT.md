# Develop OpenMuse

This guide gets a human or coding agent from a clean checkout to a safe local development loop. It keeps private credentials and saved conversations out of automated runs, then shows the checks required before a change is ready.

## Before changing code

Read the repository-root [`AGENTS.md`](../AGENTS.md) and the [OpenMuse README](./README.md). Run npm commands from `open-muse/` unless a command says otherwise.

Never read, print, or commit these files:

- `.env.local`, which can contain an API key
- `.open-muse/auth.json`, which can contain saved provider credentials
- `.open-muse/state.json`, which can contain private conversation state

## Install development dependencies

Build the local SmolVM TypeScript package from the repository root:

```bash
cd ts
npm ci
npm run build
```

Then install OpenMuse:

```bash
cd ../open-muse
npm ci
```

Create local settings without replacing an existing file:

```bash
test -f .env.local || cp .env.example .env.local
```

Rebuild `../ts` whenever its source changes. If Vite cannot resolve a dependency after `package.json` or `package-lock.json` changes, run `npm ci` again instead of patching generated files in `node_modules/`.

## Run the fast local loop

Fixture mode runs the real interface against a scripted local service. It starts no model or virtual machine and makes no public network request.

```bash
OPEN_MUSE_FIXTURE_STORE=1 npm run dev
```

Open [http://127.0.0.1:5174](http://127.0.0.1:5174).

## Isolate coding-agent state

OpenMuse restores saved state when it starts. Automated runs must use temporary credential and conversation paths so they never inherit a developer's session.

```bash
agent_state_dir="$(mktemp -d)"
OPEN_MUSE_FIXTURE_STORE=1 \
OPEN_MUSE_AUTH_PATH="$agent_state_dir/auth.json" \
OPEN_MUSE_STATE_PATH="$agent_state_dir/state.json" \
npm run dev
```

Do not reuse that temporary folder between unrelated tasks.

When another OpenMuse process is running, give browser tests their own ports:

```bash
OPEN_MUSE_E2E_APP_PORT=14318 \
OPEN_MUSE_E2E_CONTROL_PORT=14319 \
OPEN_MUSE_E2E_VIEWER_PORT=16080 \
OPEN_MUSE_E2E_CLIENT_PORT=15174 \
npm run test:e2e
```

## Run checks

Run the complete typecheck, unit-test, deterministic-evaluation, and production-build suite:

```bash
npm run check
```

Install Chromium once, then run the browser suite:

```bash
npm run test:e2e:install
npm run test:e2e
```

The browser suite drives the real OpenMuse UI against a scripted local API. It starts no model or virtual machine and makes no public network request.

### Evaluation commands

| Command | Result |
| --- | --- |
| `npm run eval:validate` | Checks the fixed tool-choice and safety corpus. |
| `npm run eval:artifact` | Writes a redacted result with case IDs and a prompt hash, never the prompts. |
| `npm run eval:live` | Runs the corpus against a configured external model. It requires credentials and stays outside pull-request CI. |

Before a milestone release, validate three independently produced live-model result files:

```bash
npm run eval:release-gate -- run-1.json run-2.json run-3.json
```

Each live run must pass every safety case, at least 90% of first-tool choices, and at least 80% of tasks. Release owners can also run the manual **OpenMuse live model release eval** GitHub Actions workflow. Its inert browser tools record requested tool names and return bounded synthetic results; they do not start a virtual machine, visit a website, or perform a browser action.

## Project map

| Path | Responsibility |
| --- | --- |
| `client/` | React chat, approvals, run details, and live-computer interface. |
| `server/` | Model access, conversation state, approvals, browser policy, and SmolVM lifecycle. |
| `test/` | Unit, integration, and browser-flow coverage. |
| `eval/` | Deterministic and live-model behavior evaluations. |
| `../ts/` | Local SmolVM TypeScript SDK used by OpenMuse. |

## Design references

- [Browser snapshots, element references, and extraction](../docs/designs/open-muse-browser-capability.md)
- [General-web security model](../docs/designs/open-muse-general-web.md)
- [Provider authentication and credential storage](../docs/designs/open-muse-provider-authentication.md)
- [Expandable agent traces](../docs/designs/openmuse-expandable-agent-traces.md)
- [Original fixture-store UI and lifecycle](../docs/designs/open-muse.md)
