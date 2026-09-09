# Network policy release validation

The new network settings keep ordinary sandbox startup fast. Correctness checks pass, and the completed open-mode performance comparison is comfortably within the proposed budget. No further performance changes are justified by these measurements.

## Decision — September 9, 2026

Release-preparation PR #498 is ready for review. This report does not publish a package or claim that every planned benchmark case completed. To keep this release small, the completed open-mode gate and existing mode measurements are sufficient; the 32-destination serial measurement remains an explicitly accepted reduction from 100 to 83 samples.

## Primary performance gate

On the same non-preemptible GCP `n2-standard-2` VM, run 100 baseline samples, 100 candidate open-mode samples, then 100 baseline samples again. Compare candidate p95 with the pooled 200-sample baseline. Percentiles use the nearest-rank method.

| Measurement | Baseline p95 | Candidate p95 | Increase | Budget | Baseline repeat drift |
| --- | ---: | ---: | ---: | ---: | ---: |
| Creation to first command | 1491.77 ms | 1504.09 ms | 12.32 ms / 0.83% | 74.59 ms | 4.47 ms |
| Creation to first network response | 1503.43 ms | 1514.89 ms | 11.46 ms / 0.76% | 75.17 ms | 3.00 ms |
| Restore to first command | 4073.97 ms | 4098.78 ms | 24.81 ms / 0.61% | 203.70 ms | 5.10 ms |

**Pass:** all increases and baseline-repeat differences are below `max(5% of baseline p95, 20 ms)`. This is evidence from one bracketed comparison, not a claim about every deployment or a statistical confidence interval.

## Other modes and concurrency

The earlier [GitHub Actions run](https://github.com/CelestoAI/SmolVM/actions/runs/34320335435) completed the following cases before cancellation. Keep these measurements separate from GCP; their absolute timings are not comparable across hosts.

| Candidate mode | Serial samples | Serial first-command p95 | Serial restore p95 | Concurrency-8 samples | Concurrent first-command p95 | Concurrent restore p95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Open | 100 | 1138.6 ms | 1766.1 ms | 24 | 2459.0 ms | 16631.8 ms |
| Off | 100 | 1193.6 ms | 1752.6 ms | 24 | 2527.0 ms | 16903.1 ms |
| Restricted, 1 destination | 100 | 1182.4 ms | 1768.2 ms | 24 | 2527.2 ms | 16574.3 ms |
| Restricted, 32 destinations | **83** | 1212.5 ms | 1786.1 ms | 24 | 2579.5 ms | 16676.7 ms |

Both concurrency baseline repeats completed (24 samples each). Their first-command p95 values were 2474.0 and 2461.1 ms; restore p95 was 16424.5 and 16621.7 ms. No separate numerical gate was proposed for restricted modes or concurrency.

## Correctness and installed-package evidence

- Firecracker and QEMU E2E passed in [run 34314767896](https://github.com/CelestoAI/SmolVM/actions/runs/34314767896); explicit off/restricted support remains Firecracker-only.
- The later controlled policy suite passed on both backends in [run 34320335435](https://github.com/CelestoAI/SmolVM/actions/runs/34320335435). Firecracker tests include actual nftables rejection before start and restore, no new guest process after rejection, and successful retry once installation works.
- Default/open, off, and restricted documentation examples passed from installed wheels, including command execution and allowed/denied HTTP probes. The GCP smoke matrix also passed all 12 two-sample cases in 402.57 seconds.
- Focused local regressions: 214 passed. Benchmark harness tests: 2 passed. Normal PR checks passed on fixture-fix commit `2450b1a`; final documentation checks are tracked on PR #498. Draft-skipped E2E/review checks are not counted as passing evidence.

## Reproduction and limitations

Baseline: `685b7bd52d0de8efb1c88fe8cf053d0d3b64d171`. Candidate wheel: `363fdc0be63eeb1a071606122de9cb5c9e518f84`. The later `2450b1a` fixture fix removes a hostname lookup from the isolated test server; it does not change the measured implementation.

Both wheels use the same pinned `images-2026.09.07.0` catalog, cached Alpine image, default two guest CPUs, 512 MiB guest RAM, vsock commands, controlled literal-IP HTTP endpoint, and non-root sudo networking fallback. The GCP serial run used a separate 20 GB `pd-ssd` scratch disk. Its existing PostgreSQL container was left untouched; baseline repetition checks for drift, but this is not an isolated bare-metal result. Exact CPUs and dependency versions are in the saved environment metadata.

Each case has an unrecorded warmup. Creation includes configuration, start, and first command; first request is cumulative from creation. Snapshot preparation and stop/delete cleanup are outside measured spans. Batches complete creation, then snapshot/delete all sources, then restore together. UUID names are substituted on both versions to avoid an existing automatic-name collision. This does not validate arbitrary interleaving of fresh starts and restores or fix the separate naming/in-place-restore issues.

GCP reclaimed the first, preemptible N1 run. Its partial serial baseline is excluded from the gate. All primary comparisons above were rerun on N2. After that gate passed, the redundant N2 off-mode run was intentionally interrupted; its pytest exit code is therefore nonzero and is not presented as a completed full matrix. No failed or interrupted case is represented as 100 successful samples.

## Saved evidence

- [Raw measured samples](network-policy-validation/timings.csv): 927 samples, labeled by source and case, including the partial 83-sample CI case.
- [Per-case summaries](network-policy-validation/summary.json).
- [Primary gate calculation](network-policy-validation/primary-gate.json).
- [Hardware, wheel revisions, and installed dependencies](network-policy-validation/environment.json).

The plan's next steps are PR review/merge and the normal package-release checklist. No proxy, new policy API, guest-image change, or further optimization is part of this PR.
