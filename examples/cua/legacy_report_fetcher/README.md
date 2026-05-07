# Legacy report fetcher computer-use demo

This demo shows a computer-use agent logging into a legacy reporting portal, downloading files through a browser, and handing those files to an existing pipeline.

The whole workflow runs inside a SmolVM browser sandbox. The demo folder is mounted into the sandbox, the fake legacy app starts from the sandbox shell, the agent controls the browser over CDP, and all file work happens through `vm.run()` commands inside the sandbox.

## What it demonstrates

- A browser agent can use a legacy web app when there is no API.
- SmolVM gives that agent an isolated computer with a live browser view.
- The agent uses the browser for clicks and downloads.
- The sandbox shell handles files, hashes, manifests, and pipeline handoff.
- A writable mount makes output appear on the host for live calls and recordings.

## Run it

This demo needs two host-side Python packages: `openai` for the computer-use model and `playwright` to connect to the browser over CDP.

```bash
export OPENAI_API_KEY=...
uv run --with openai --with playwright examples/cua/legacy_report_fetcher/run_demo.py --mode live
```

Open the printed live URL to watch the browser.

For a recording-friendly run, keep `--mode live`. For a quieter run:

```bash
uv run --with openai --with playwright examples/cua/legacy_report_fetcher/run_demo.py --mode headless
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

Browser session logs and video are collected in the SmolVM browser artifacts directory printed by the script.

## Fake portal credentials

```txt
username: ops@acme.test
password: demo-password
```

## Architecture

```txt
Host folder mounted writable into sandbox
└── /workspace/legacy_report_fetcher
    ├── portal/      # fake Acme Legacy Reports Portal
    ├── ops/         # sandbox-side operational scripts
    ├── pipeline/    # existing-pipeline stand-in
    └── artifacts/   # output visible on the host
```

The browser opens the portal at:

```txt
http://127.0.0.1:8000
```

That server runs inside the SmolVM sandbox, not on the host laptop.
