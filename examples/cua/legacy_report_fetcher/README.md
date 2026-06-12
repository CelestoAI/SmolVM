# Legacy report fetcher computer-use demo

This demo shows an agent using a browser to get reports from an old web app and hand those reports to a normal data pipeline.

The workflow runs in a SmolVM browser sandbox. The fake Acme portal starts inside the sandbox, the agent uses the browser, the sandbox checks the files, and the final reports are copied back to your machine for the pipeline.

## What it demonstrates

- An agent can use a legacy web app when there is no API.
- SmolVM gives the agent an isolated computer with a live browser you can watch.
- The workflow does not trust the agent saying “done”; it checks that the files exist.
- The sandbox shell can list and recover report files when browser downloads are unreliable.
- The harness copies verified files from the sandbox to the host before running the pipeline.

## Run it

This demo needs two host-side Python packages: `openai` for the computer-use model and `playwright` to connect to the sandbox browser over CDP.

```bash
export OPENAI_API_KEY=...
uv run --with openai --with playwright examples/cua/legacy_report_fetcher/run_demo.py --mode live
```

Open the printed live URL to watch the browser.

For a quieter run without the live browser view:

```bash
uv run --with openai --with playwright examples/cua/legacy_report_fetcher/run_demo.py --mode headless
```

Optional flags:

```bash
--report-date 2026-05-07   # defaults to yesterday
--max-steps 24             # max computer-use turns
--boot-timeout 180         # seconds to wait for the sandbox to boot
```

## What you should see

The script prints progress as it moves through the workflow:

```txt
Starting SmolVM browser sandbox...
Starting Acme legacy portal inside the sandbox...
Starting OpenAI computer-use browser task...
Listing sandbox downloads and copying reports to the host...
Running the existing-pipeline import on the host handoff folder...
```

A successful run ends with pipeline output like:

```txt
Found manifest.json for acme_legacy_reports
Imported orders_2026-05-07.csv: 3 rows into orders
Imported inventory_2026-05-07.csv: 3 rows into inventory
Stored normalized data in .../artifacts/warehouse/acme.sqlite
Run complete.
```

## Output

Generated files appear under:

```txt
examples/cua/legacy_report_fetcher/artifacts/
  inbox/acme/<report-date>/
    orders_<report-date>.csv
    inventory_<report-date>.csv
    manifest.json
  warehouse/
    acme.sqlite
  screenshots/
    01-portal-login.png
    02-after-downloads.png
```

Browser sandbox logs and video are collected in the SmolVM browser artifacts directory printed by the script.

## Fake portal credentials

```txt
username: ops@acme.test
password: demo-password
```

## How it works

```txt
Host folder mounted into sandbox
└── /workspace/legacy_report_fetcher
    ├── portal/      # fake Acme Legacy Reports Portal
    ├── ops/         # sandbox-side helper scripts
    ├── pipeline/    # existing-pipeline stand-in
    └── artifacts/   # host-side handoff output
```

The browser opens the portal at:

```txt
http://127.0.0.1:8000
```

That server runs inside the SmolVM sandbox, not on the host laptop.

The important reliability step is after the agent finishes. The harness lists the sandbox download folder, recovers files if needed, copies the verified CSVs to the host, writes `manifest.json`, and then runs the pipeline.
