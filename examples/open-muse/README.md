# Open Muse

Open Muse turns one travel goal into a sourced itinerary, budget, and research packet. The AI works inside a temporary SmolVM, copies out the finished files, and deletes the temporary computer.

This is a focused local demo, not a general personal assistant. It supports one operator, one run at a time, and the built-in three-day Bengaluru trip workflow.

## Run the demo

You need Node.js 22.19 or newer, an OpenAI API key, and a working SmolVM installation.

Install and check SmolVM first:

```bash
curl -sSL https://celesto.ai/install.sh | bash
smolvm doctor
```

Then install the example. `--allow-remote=root` lets npm 12 fetch the SmolVM release archive named directly in this package.

```bash
cd examples/open-muse
npm install --allow-remote=root
```

Create your local configuration:

```bash
cp .env.example .env.local
```

Add your API key to `.env.local`:

```dotenv
OPENAI_API_KEY=your-key-here
```

Start the app:

```bash
npm run dev
```

If the TypeScript SDK reports that the installed SmolVM runtime is too old while developing from this repository, use the source runtime:

```dotenv
SMOLVM_RUNTIME=./scripts/source-smolvm-runtime.sh
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173). Prepare the built-in plan, review its three steps, then select **Start research**.

## What you receive

- `brief.md` summarizes the recommendation, assumptions, and unverified details.
- `itinerary.md` groups three days of activities into walkable neighborhoods.
- `budget.csv` itemizes costs in INR and adds a 10% contingency.
- `sources.json` records each source URL, retrieval time, title, and content hash.
- `Open-Muse-packet.zip` contains all four files.

Open Muse keeps verified files in server memory after it deletes the VM. Stopping the Node server removes those in-memory results.

## Safety boundaries

The Pi agent harness receives only four purpose-built tools: fetch a public page, record a finding, calculate a budget, and write an approved artifact. It never receives a shell tool. Each operation validates its input on the host and runs a fixed script inside the VM with an argument array.

The OpenAI API key stays in the Node server. The VM receives no host folders, cookies, browser profile, SSH keys, cloud credentials, or model credentials. The server listens on `127.0.0.1` and rejects non-loopback configuration.

The VM uses public-web network access for research. Treat the downloaded packet as AI-generated material and verify important prices before booking.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | none | Authorizes the server's OpenAI requests. |
| `OPENAI_MODEL` | `gpt-5-mini` | Selects a model from the pinned Pi catalog. |
| `OPEN_MUSE_HOST` | `127.0.0.1` | Loopback address for the control server. Other addresses are rejected. |
| `OPEN_MUSE_PORT` | `4317` | Port used by the control server. |

## Verify changes

Run the complete local check without a VM or model request:

```bash
npm run check
```

The tests use structural fake SmolVM and workflow implementations. They cover artifact validation, URL policy, planning output, one-run enforcement, cancellation, export, and cleanup.

## Current limits

- The live workflow is tuned for the built-in Bengaluru trip goal.
- Results are not persisted after the server stops.
- A run can fetch at most eight public HTTPS pages and lasts at most eight minutes.
- Completed artifacts remain available for download for 15 minutes, then the server releases them.
- Logged-in sites, purchases, forms, messages, host files, and arbitrary commands are unavailable.
- The real-VM and live-model release smoke tests remain manual because normal CI must not depend on travel websites or paid model calls.

See the [implementation handoff](../../docs/designs/open-muse.md) for the product contract, architecture, and remaining release work.
