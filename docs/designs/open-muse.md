# Open Muse: Implementation Handoff

Open Muse is a local web app that accepts one personal goal, researches it inside a disposable computer, and returns a small packet of useful files. The first demo plans a three-day Bengaluru trip for two people under ₹40,000, showing each stage of the work before deleting the temporary computer.

Generated from the Open Muse office-hours session on 2026-09-12.  
Branch: `codex/open-muse-handoff`  
Repository: `CelestoAI/SmolVM`  
Status: IMPLEMENTED — live OpenAI and release-host validation pending
Mode: time-boxed open-source demo

## Outcome

A developer can clone SmolVM, configure one OpenAI API key, start Open Muse, run the built-in trip-planning goal, and download a ZIP containing:

- `brief.md`: assumptions, constraints, and a concise recommendation.
- `itinerary.md`: a three-day schedule grouped by neighborhood.
- `budget.csv`: itemized costs in INR with category totals and contingency.
- `sources.json`: every source URL, retrieval time, title, and content hash.

The browser shows the goal, plan, live VM activity, artifact status, and cleanup result. It does not display hidden model reasoning.

## Why build this

Meta describes Muse as an agent that turns goals into plans, works through a dedicated VM, uses a browser, and pauses when it needs approval. That full product also includes messaging channels, connected accounts, memory, payments, and background operation. Open Muse keeps only the smallest complete loop: give it a goal, watch it work in its own computer, keep the result, and discard the computer.

Current open-source personal agents tend to optimize for breadth. OpenClaw connects many channels and tools through a gateway, while OpenManus combines a general agent loop with separate browser tooling. Open Muse should not compete on integrations. Its demo claim is narrower:

> Local does not mean the agent can touch your laptop. Every model-directed operation runs through a fixed tool inside a disposable SmolVM.

Sources:

- [Meta: Introducing Muse](https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/)
- [OpenClaw](https://github.com/openclaw/openclaw/blob/main/README.md)
- [OpenManus](https://github.com/FoundationAgents/OpenManus/blob/main/README.md)
- [Pi agent core](https://github.com/earendil-works/pi/tree/main/packages/agent)
- [Pi agent tool execution](https://github.com/earendil-works/pi/blob/main/packages/agent/README.md#tools)

## Product decisions

- Build a time-boxed, open-source demo rather than a general personal assistant.
- Make goal-to-research-packet the only polished workflow.
- Ship a local web app, not a hosted service or terminal-only program.
- Use a React/Vite interface and a dedicated Node server. The Node server owns the agent loop and every SmolVM handle.
- Support one trusted local operator and one active run at a time.
- Use OpenAI as the first tested provider through the Pi agent harness. Keep the model ID configurable.
- Allow public-web research, but give the model only purpose-built tools. Do not expose a general shell tool.
- Keep accounts, purchases, email, persistent memory, background jobs, and remote access out of the first release.

## Success criteria

The demo is done when all of these are true:

1. On a supported machine with SmolVM installed and its image cached, a new developer can reach the finished packet in under ten minutes from `git clone`.
2. The UI emits its first lifecycle event within two seconds of starting a run.
3. The built-in goal produces all four required files and a downloadable ZIP.
4. `budget.csv` parses successfully, uses INR, totals at or below ₹40,000, and includes a contingency line.
5. Every factual or price claim in the Markdown output refers to an entry in `sources.json`.
6. Success, cancellation, model failure, tool failure, client disconnect, and server shutdown leave no session-owned VM running.
7. No model-selected command runs on the host. The OpenAI API key never enters the VM.
8. The server listens on loopback by default and refuses a non-loopback bind unless a future design explicitly adds authentication.
9. Unit tests run without a VM or network by using the SDK's structural mock interfaces.
10. One real-VM smoke test exercises the packed TypeScript SDK artifact on Apple Silicon macOS and Linux x64 before the demo is announced.

## User journey

### 1. Start

The empty screen explains the outcome in one sentence and offers the built-in goal:

> Plan a three-day Bengaluru trip for two under ₹40,000. Prioritize local food, architecture, and walkable neighborhoods. No nightlife.

The user may edit the constraints before starting. Do not imply that arbitrary goals have equal support.

### 2. Review the plan

Open Muse returns a short, structured plan before using tools:

1. Gather current travel and stay estimates.
2. Compare walkable neighborhoods and activities.
3. Build and verify the itinerary and budget.

The user selects **Start research** or edits the constraints. This is plan confirmation, not permission for purchases or account access. Those actions do not exist in this release.

### 3. Watch the work

The left pane keeps the goal and constraints visible. The right pane shows:

- current phase and elapsed time;
- SmolVM startup, image download, and sandbox readiness;
- a curated command transcript containing executable name, purpose, duration, and exit status;
- sources collected and artifacts written;
- a **Stop** action that cancels the model loop and cleans up the sandbox.

Do not render chain-of-thought, model tokens, bearer credentials, complete environment variables, or raw fetched page contents.

### 4. Inspect the result

When verification passes, show a preview for each artifact and enable **Download packet**. Keep the result available after the VM has been deleted. The final timeline entry must say either:

- `Temporary computer deleted`, or
- `Cleanup needs attention. Run: <exact recovery command>`.

### 5. Recover from failure

Keep completed artifacts visible when a later phase fails. Show one short fact and one action. Examples:

- `Open Muse could not start a private computer. Run 'smolvm doctor' and try again.`
- `Research stopped after 8 minutes. Try again with fewer constraints.`
- `The model did not produce a valid budget. Retry the verification step.`

## Approved layout

The approved wireframe uses a two-pane desktop layout:

![Approved Open Muse wireframe](../assets/open-muse-wireframe.png)

```text
┌──────────────────────────────┬───────────────────────────────────────────────┐
│ Your goal                    │ Muse's computer                              │
│                              │                                               │
│ Trip goal and constraints    │ VM status + curated command transcript        │
│                              │                                               │
│ Muse's plan                  │ Lifecycle and research timeline               │
│                              │                                               │
│ Add constraint / Stop        │ brief.md  itinerary.md  budget.csv  sources   │
│                              │ Download packet                               │
└──────────────────────────────┴───────────────────────────────────────────────┘
```

On narrow screens, stack the goal above the work area. The artifact list remains visible when the run completes.

Required visual states:

- empty and ready;
- preparing the plan;
- waiting for plan confirmation;
- starting runtime and downloading image;
- researching;
- building artifacts;
- verifying;
- complete and cleaning up;
- cancelled;
- recoverable failure;
- cleanup failure.

## Architecture

```text
Browser on 127.0.0.1
        │ HTTP + server-sent events
        ▼
Node control server
  ├── run registry: one in-memory RunState
  ├── Pi Agent: capped model/tool loop
  ├── purpose-built tool policy
  ├── artifact export and ZIP response
  └── SmolVM client: sole owner of sandbox lifecycle
        │ authenticated private bridge
        ▼
Disposable Ubuntu VM
  ├── fixed research scripts
  ├── fetched public pages
  ├── intermediate structured data
  └── four verified output files
```

Vite serves the React app during development and proxies `/api` to the Node server. For the demo build, the Node server serves Vite's static output so `npm start` launches one long-lived process. Do not deploy this server to a serverless runtime: it owns a child process and a local VM.

## Proposed repository layout

Create the app as an isolated example so it does not turn the repository root into an npm workspace:

```text
examples/open-muse/
├── README.md
├── package.json
├── package-lock.json
├── tsconfig.json
├── vite.config.ts
├── .env.example
├── client/
│   ├── main.tsx
│   ├── App.tsx
│   ├── api.ts
│   ├── styles.css
│   └── components/
│       ├── GoalPanel.tsx
│       ├── WorkTimeline.tsx
│       ├── ComputerEvidence.tsx
│       └── ArtifactPanel.tsx
├── server/
│   ├── index.ts
│   ├── config.ts
│   ├── errors.ts
│   ├── events.ts
│   ├── run-manager.ts
│   ├── agent.ts
│   ├── tools.ts
│   ├── artifact-contract.ts
│   └── scripts/
│       ├── fetch_page.py
│       ├── calculate_budget.py
│       ├── verify_packet.py
│       └── package_packet.py
└── test/
    ├── run-manager.test.ts
    ├── tools.test.ts
    ├── artifacts.test.ts
    └── smoke.test.ts
```

Use the released `@celestoai/smolvm` tarball initially. Do not import from `ts/src` or depend on repository-relative build output. This keeps the example honest about the public installation path.

## Runtime ownership

`RunManager` is the only component allowed to construct `SmolVM`. Keep one active `RunContext`:

```ts
type RunPhase =
  | "planning"
  | "awaiting_plan_approval"
  | "starting_sandbox"
  | "researching"
  | "building_packet"
  | "verifying"
  | "exporting"
  | "cleaning_up"
  | "complete"
  | "cancelled"
  | "failed";

interface RunContext {
  id: string;
  phase: RunPhase;
  goal: string;
  constraints: string[];
  startedAt: string;
  abortController: AbortController;
  smolvm?: SmolVM;
  sandbox?: SandboxClient;
  artifacts: Partial<Record<ArtifactName, Uint8Array>>;
  events: RunEvent[];
}
```

Rules:

- Reject a second active run with HTTP `409` and a plain-English response.
- Construct SmolVM only after plan confirmation.
- Forward typed `SmolVMEvent` values into the run event stream through an explicit mapper.
- Keep at most 500 events. Coalesce repeated `image.download` progress updates.
- Copy verified artifacts to bounded host memory before deleting the VM. The complete packet must remain below 8 MiB, leaving margin under the SDK's 16 MiB file transfer limit.
- Call `smolvm.close()` in `finally` for every terminal path.
- Install `SIGINT`, `SIGTERM`, `uncaughtException`, and `unhandledRejection` handlers that stop accepting work and await one bounded cleanup attempt before exit.
- Rely on the SDK control pipe as the final safety net if the Node process is killed before JavaScript cleanup runs.

## Agent loop

Use Pi's `Agent` from `@earendil-works/pi-agent-core`, its OpenAI provider from `@earendil-works/pi-ai`, and TypeBox schemas for every model tool. Pin both Pi packages in `package-lock.json`. Configure:

- `shouldStopAfterTurn` with a 12-turn hard ceiling;
- eight minutes as the total run deadline;
- 30 seconds per VM command;
- at most eight fetched sources;
- at most one retry for a malformed final artifact;
- the run's `AbortSignal` for model calls and every sandbox command.

The first provider is OpenAI. Read `OPENAI_API_KEY` and `OPENAI_MODEL` only in the Node server. Never return either value through configuration, diagnostics, events, or tool results. Keep provider construction behind a small `createModel(config)` function so a later provider does not change the agent or tool contracts.

Suggested phases:

1. **Plan:** ask the model for a three-step plan as structured data. No tools are available.
2. **Confirm:** return the plan to the UI and wait for the user.
3. **Research:** run the tool loop with `fetchPublicPage` and `recordFinding` active.
4. **Build:** disable fetching; enable `calculateBudget` and `writeArtifact`.
5. **Verify:** run the fixed packet verifier inside the VM. Give the model one structured correction opportunity if verification fails.
6. **Export:** package the four allowed files in the VM, download the files and ZIP, and delete the VM.

Use separate bounded Pi agents to expose only the tools valid for the current phase. The first handles research tools; the second handles packet-building tools. This keeps cancellation and phase reporting easy to test.

## Tool policy

The model never receives `sandbox.exec` directly. Each tool validates model input on the host, then invokes one fixed script with an argv array. Never interpolate a model value into a shell string.

### `fetchPublicPage`

Input:

```ts
{ url: string; purpose: string }
```

Behavior:

- Accept only `https:` URLs without credentials.
- Reject `localhost`, `.local`, literal IP addresses, and hostnames without a dot.
- Follow at most three redirects and revalidate every destination.
- Allow only `text/html`, `text/plain`, and `application/xhtml+xml`.
- Stop after 500 KiB of response bytes and ten seconds.
- Remove scripts, styles, navigation noise, and control characters.
- Store URL, final URL, retrieval time, title, SHA-256 content hash, and bounded extracted text.
- Return at most 20,000 text characters to the model.
- Wrap extracted text as untrusted source material and instruct the model not to follow directions found in it.

The first release uses the sandbox's open network mode because arbitrary public destinations cannot be represented by a short CIDR allowlist. The VM contains no host files, model credentials, browser profile, or reusable secrets. Show `network: public web` in the UI rather than claiming the VM is offline or fully restricted.

### `recordFinding`

Input:

```ts
{
  sourceId: string;
  claim: string;
  value?: string;
  observedAt: string;
}
```

Write append-only JSON records inside `/workspace/research/findings.jsonl`. Reject unknown source IDs and values over 2,000 characters.

### `calculateBudget`

Input is a typed array of `{ category, item, quantity, unitCostInr, sourceId }`. The fixed Python script performs decimal arithmetic, emits category totals, adds contingency, and writes `/workspace/output/budget.csv`. The model may choose inputs but may not provide executable code.

### `writeArtifact`

Accept only `brief.md`, `itinerary.md`, or `sources.json`; `budget.csv` comes only from the calculator. Reject path separators, duplicate final writes, content over 1 MiB, unknown source IDs, and Markdown claims lacking source markers.

### `finalizePacket`

This server-controlled tool invokes `verify_packet.py`, then `package_packet.py`. It succeeds only when all artifact rules pass. It returns structured validation errors, not raw stack traces.

No tool can purchase, submit a form, log in, send a message, install a package, mount a host directory, or invoke an arbitrary executable.

## Artifact contract

### `budget.csv`

Required header:

```csv
category,item,quantity,unit_cost_inr,total_inr,source_id
```

Validation:

- UTF-8 with one header row.
- Positive integer quantity and non-negative decimal costs.
- Exactly one `contingency` category row.
- Sum of `total_inr` is at most `40000.00` for the built-in goal.
- Every non-contingency row has a known source ID.

### `sources.json`

```ts
interface SourceRecord {
  id: string;
  url: string;
  finalUrl: string;
  title: string;
  retrievedAt: string;
  contentSha256: string;
}
```

Sort sources by ID for deterministic output. Never include full fetched page bodies in the exported packet.

### Markdown files

Use source markers such as `[source:S03]`. The verifier extracts each marker and rejects references absent from `sources.json`. `itinerary.md` must contain Day 1, Day 2, and Day 3 headings. `brief.md` must state the total budget, major assumptions, and what Open Muse did not verify.

## Local API

All routes bind to `127.0.0.1` by default.

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/health` | Report readiness without secrets. |
| `POST` | `/api/plans` | Validate a goal and return the structured plan. |
| `POST` | `/api/runs` | Confirm a plan and start its run. |
| `GET` | `/api/runs/:id` | Return the current public run snapshot. |
| `GET` | `/api/runs/:id/events` | Stream ordered run events as server-sent events. |
| `POST` | `/api/runs/:id/constraints` | Queue one constraint for the next phase boundary. |
| `POST` | `/api/runs/:id/cancel` | Abort work and begin cleanup. |
| `GET` | `/api/runs/:id/artifacts/:name` | Download one verified artifact. |
| `GET` | `/api/runs/:id/packet.zip` | Download the verified packet. |

Generate run IDs server-side. Validate all bodies with Zod. Limit JSON request bodies to 64 KiB, goals to 1,000 characters, constraints to 20 entries, and each constraint to 300 characters.

SSE event names:

```ts
type RunEvent =
  | { type: "run.phase"; phase: RunPhase; at: string }
  | { type: "plan.ready"; steps: string[]; at: string }
  | { type: "vm.lifecycle"; name: string; progress?: number; at: string }
  | { type: "tool.started"; tool: string; purpose: string; at: string }
  | { type: "tool.completed"; tool: string; durationMs: number; summary: string; at: string }
  | { type: "source.saved"; source: Pick<SourceRecord, "id" | "url" | "title">; at: string }
  | { type: "artifact.ready"; name: ArtifactName; bytes: number; at: string }
  | { type: "run.warning"; message: string; recovery?: string; at: string }
  | { type: "run.failed"; message: string; recovery?: string; at: string }
  | { type: "run.completed"; durationMs: number; at: string };
```

Send an SSE heartbeat every 15 seconds. On reconnect, accept `Last-Event-ID` and replay retained events. Disconnecting the browser does not cancel the run; the user must press **Stop** or stop the Node server.

## Error mapping

Translate `SmolVMError.code` at the server boundary. Preserve the code in debug logs, but keep UI messages short and actionable. Do not send causes, tokens, environment dumps, or guest stack traces to the browser.

At minimum, map:

- unsupported Node or protocol mismatch → rerun the official installer;
- missing runtime → install SmolVM;
- backend unavailable → run `smolvm doctor` and show its exact backend recovery when available;
- image or sandbox creation failure → retry after the reported recovery command;
- timeout or abort → state whether the sandbox was deleted;
- bridge exit or cleanup failure → stop the run and show the exact local cleanup command.

## Security boundaries

- Treat the goal, fetched pages, model output, and artifact content as untrusted data.
- The browser never receives the model API key or SmolVM bridge credential.
- The VM receives no host paths, mounted folders, cookies, browser profile, SSH keys, cloud credentials, or model credentials.
- Use argv arrays for every sandbox command.
- Keep all guest paths under `/workspace/open-muse`; validate paths before SDK calls.
- Do not render generated HTML. Show Markdown as escaped text or through a renderer that strips raw HTML, scripts, images, and embedded links by default.
- Add `Content-Security-Policy`, `X-Content-Type-Options: nosniff`, and `Referrer-Policy: no-referrer` headers.
- Set `Cache-Control: no-store` for API responses and artifacts.
- Do not add telemetry. Logs stay local and redact URLs' query strings.
- Do not claim that public-web access is safe against every network target. This is a single-operator demo running inside an isolated VM, not a hostile multi-user service.

## Cancellation and cleanup

Cancellation is a product feature, not a best-effort button:

1. Abort the active model request.
2. Pass the same `AbortSignal` to the active sandbox command.
3. Stop scheduling tools.
4. Preserve already downloaded, verified artifacts.
5. Call `smolvm.close()` and await completion.
6. Emit the cleanup outcome, then mark the run cancelled or failed.

The cleanup deadline is 15 seconds. If cleanup throws, call `close()` once more because it is idempotent. If the second attempt fails, emit one recovery command and keep the server in a degraded state that rejects new runs. Do not report completion while ownership is uncertain.

## Testing strategy

### Unit tests

- Run-state transitions and rejection of illegal transitions.
- One-active-run enforcement.
- Event ordering, replay, heartbeat, and progress coalescing.
- URL validation, redirect revalidation, byte limits, content-type limits, and query-string redaction.
- Tool schemas and fixed argv construction.
- Artifact path, size, citation, day-heading, and budget validation.
- Error mapping and secret redaction.
- Idempotent cancellation and cleanup.

Use fake `SmolVMClient`, `SandboxClient`, and model interfaces. Tests must prove that different tool inputs reach the fake sandbox rather than returning fixture constants.

### Integration tests without a real VM

- Start the Node server on an ephemeral loopback port.
- Exercise plan, run, SSE reconnect, constraint injection, cancellation, artifact download, and second-run rejection.
- Use deterministic model responses and an in-memory fake sandbox.
- Build the React app and verify the Node server serves its output.

### Real-VM smoke

- Install the packed TypeScript SDK artifact into the example.
- Use a deterministic fake model that requests the real fixed guest scripts.
- Fetch one stable public HTML fixture, calculate a small budget, write all outputs, download the ZIP, and close the SDK.
- Verify no process-local sandboxes remain after success and after cancellation.

Do not make normal CI depend on travel websites or live model APIs. Keep the live Bengaluru run as a manual release demo.

## Delivery milestones

### Milestone 1: Vertical slice

- Create `examples/open-muse` with Vite, React, Node server, and one command to start both.
- Add configuration validation and the empty goal screen.
- Start a real SmolVM, write a fixture artifact, download it, and clean up.
- Render lifecycle events in the timeline.

Exit test: entering the built-in goal returns one downloaded `brief.md` from a real VM.

### Milestone 2: Bounded agent loop

- Add Pi `Agent`, OpenAI configuration, plan generation, and plan confirmation.
- Implement the four purpose-built tools and fixed guest scripts.
- Add run deadline, step cap, source cap, and cancellation.

Exit test: a deterministic model completes the four-file packet without arbitrary shell access.

### Milestone 3: Artifact verification

- Implement schemas and guest verification.
- Add one correction pass.
- Package and download the ZIP before cleanup.
- Preview artifacts safely in the UI.

Exit test: malformed citations or an over-budget CSV fail with a useful recovery; a valid packet downloads after the VM is deleted.

### Milestone 4: Demo polish

- Match the approved two-pane hierarchy.
- Implement every required state, responsive stacking, keyboard focus, and reduced-motion behavior.
- Add the curated computer transcript and elapsed-time display.
- Write the example README and record the live demo.

Exit test: a fresh supported machine completes the documented path in under ten minutes.

## Installation and distribution

The example remains in this repository and uses npm. Its README should lead with:

```bash
curl -sSL https://celesto.ai/install.sh | bash
cd examples/open-muse
cp .env.example .env.local
npm install --allow-remote=root
npm run dev
```

The example package depends on the checksummed TypeScript preview tarball until registry publication. npm 12 blocks remote tarballs by default, so use `npm install --allow-remote=root`; `root` permits only URL dependencies named directly by this package. Pi requires Node.js 22.19 or newer.

CI must run typechecking, unit tests, production build, fake-runtime integration tests, packed-SDK install tests, and supported-host real-VM smoke tests. The demo has no separate deployment: users clone the repository and run it locally.

## Explicitly deferred

- General browser control and visual click automation.
- Logged-in websites, credentials, purchases, and form submission.
- Arbitrary goals with a reliability claim.
- Persistent sandboxes, memory, schedules, and work after the app closes.
- Multiple users, multiple concurrent runs, remote access, and cloud deployment.
- Messaging channels, mobile clients, voice, and notifications.
- Local-model inference and automatic provider discovery.
- Plugins, skills marketplace, and third-party tool installation.

## Open questions

Resolve these during Milestone 1 without expanding scope:

1. Select and pin the first tool-capable OpenAI model after a live compatibility probe; keep `OPENAI_MODEL` configurable.
2. Choose whether queued constraints are accepted during all phases or only before artifact generation. Default to phase boundaries only.
3. Decide whether the curated command transcript shows full public URLs or only hostnames. Default to hostnames to reduce accidental query-string disclosure.

## Next engineer checklist

1. Create the example package and its lockfile.
2. Implement `RunManager` with a fake SmolVM and fake model first.
3. Build one real-VM vertical slice before styling beyond the approved layout.
4. Add each model tool only after its validator and failure tests exist.
5. Make success, cancel, and forced server shutdown pass the no-leaked-VM test.
6. Run the built-in goal live, inspect every exported claim and price, and record cold/warm timings.
7. Update this document when an approved decision changes; do not silently expand deferred scope.

## What I noticed

- The idea became sharper when “something like Muse” changed into one concrete result: a research packet.
- Choosing a local web app kept the product feeling of Muse while preserving SmolVM's local execution story.
- Choosing the middle architecture put effort into the visible experience without introducing a worker system that the demo does not need.
