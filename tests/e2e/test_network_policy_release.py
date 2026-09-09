"""Opt-in release measurements using the existing disposable network lab."""

from __future__ import annotations

import json
import math
import os
import re
import statistics
import subprocess
import time
from pathlib import Path

import pytest
from test_network_policy import policy_lab  # noqa: F401

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("SMOLVM_POLICY_BASELINE_PYTHON"),
        reason="Release matrix is opt-in through the E2E workflow dispatch",
    ),
]


def test_installed_examples_and_performance(policy_lab, tmp_path):  # noqa: F811
    allowed, denied, *_ = policy_lab
    root = Path(__file__).resolve().parents[2]
    results = Path(os.environ["SMOLVM_POLICY_RESULTS"])
    results.mkdir(parents=True, exist_ok=True)
    interpreters = {
        version: os.environ[f"SMOLVM_POLICY_{version.upper()}_PYTHON"]
        for version in ("baseline", "candidate")
    }
    samples = int(os.environ.get("SMOLVM_POLICY_SAMPLES", "100"))

    # Execute the actual documented context-manager examples from an installed
    # wheel, outside the checkout. Only the illustrative destination is replaced.
    guide = (root / "docs/guides/networking.md").read_text()
    examples = [
        block.replace("203.0.113.10", allowed)
        for block in re.findall(r"```python\n(.*?)```", guide, re.DOTALL)
        if "with SmolVM(" in block and '"mode":' in block
    ]
    assert len(examples) == 2
    examples.insert(
        0,
        'from smolvm import SmolVM\nwith SmolVM(backend="firecracker", '
        'comm_channel="vsock") as vm:\n    print(vm.run("echo hello").stdout)\n',
    )
    with (results / "installed-examples.log").open("w") as log:
        for mode, code in zip(("open", "off", "restricted"), examples, strict=True):
            # Keep probes in the documented `with` scope, before cleanup.
            code += (
                '    assert vm.run("printf control-ok").stdout == "control-ok"\n'
                f'    reply = vm.run("wget -T 1 -qO- http://{allowed}:18080/example-{mode}")\n'
                f"    assert (reply.exit_code == 0) is {mode != 'off'}\n"
            )
            if mode != "open":
                code += (
                    f'    assert vm.run("wget -T 1 -qO- '
                    f'http://{denied}:18080/example-denied").exit_code != 0\n'
                )
            code = (
                "import smolvm\nfrom pathlib import Path\n"
                'assert "site-packages" in str(Path(smolvm.__file__).resolve())\n'
                "print(smolvm.__file__, flush=True)\n" + code
            )
            log.write(f"\n{mode}\n")
            log.flush()
            subprocess.run(
                [interpreters["candidate"], "-c", code],
                cwd=tmp_path,
                check=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=600,
            )

    summary = {}
    script = root / "scripts/benchmark-network-policy.py"
    for concurrency in (1, 8):
        # Bracket each matrix with baseline repeats to expose runner drift.
        cases = [
            ("baseline-before", "baseline", "open", []),
            ("candidate-open", "candidate", "open", []),
            ("candidate-off", "candidate", "off", []),
            ("candidate-restricted-1", "candidate", "restricted", [allowed]),
            (
                "candidate-restricted-32",
                "candidate",
                "restricted",
                # Nonadjacent addresses stay at 32 entries after normalization.
                [allowed, *(f"203.0.113.{2 * n}/32" for n in range(1, 32))],
            ),
            ("baseline-after", "baseline", "open", []),
        ]
        for label, version, mode, destinations in cases:
            name = f"{label}-c{concurrency}"
            print(f"Measuring {name}: {samples} samples", flush=True)
            output = results / f"{name}.jsonl"
            command = [
                interpreters[version],
                str(script),
                "--mode",
                mode,
                "--samples",
                str(samples),
                "--concurrency",
                str(concurrency),
                "--restore",
                "--url",
                f"http://{allowed}:18080/benchmark",
                "--data-dir",
                str(tmp_path / "benchmark-data"),
                "--output",
                str(output),
            ]
            for destination in destinations:
                command.extend(("--allow", destination))
            started = time.monotonic()
            with (results / f"{name}.log").open("w") as log:
                subprocess.run(
                    command,
                    cwd=tmp_path,
                    check=True,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=2400,
                )
            rows = [json.loads(line) for line in output.read_text().splitlines()][1:]
            assert len(rows) == samples
            metrics = {"elapsed_seconds_including_warmup": time.monotonic() - started}
            for metric in rows[0].keys() - {"sample"}:
                values = sorted(row[metric] for row in rows)
                metrics[metric] = {
                    "median_ms": statistics.median(values),
                    "p95_ms": values[math.ceil(0.95 * len(values)) - 1],
                }
            summary[name] = metrics
            (results / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
