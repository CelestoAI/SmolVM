# QEMU network controls: validation summary

Local applications and shared folders work with the new network controls. Several performance checks passed, but the remaining gaps below mean this change is not fully cleared for production.

## Evidence status

The benchmarked implementation is recorded in commit `242bf79`, against the unchanged v0.0.32 baseline (`0ab99c7`). Those measurements predate the port-reservation fix in `aea10eb`; they are not a benchmark of the latest code.

After that fix, the local regression suite passed **2,227 tests**, with 21 skipped and 49 integration cases deselected. Lint passed. The mocked lifecycle tests now explicitly reject attempts to invoke `qemu-img`.

Earlier installed-wheel checks passed:

- Linux QEMU/KVM: 12 TAP application tests, 6 slirp tests, and both Firecracker/QEMU packet-contract cases.
- Linux full-memory restore: TAP off, TAP address restrictions, and slirp off.
- macOS QEMU/HVF: 6 application tests; Linux-only cases skipped.

These checks covered HTTP, file transfers, WebSockets, SDK operations, shared folders, restart, resume, restore, and failed firewall installation/retry. The new port-reservation behavior has local regression coverage but has not been rerun in the real Linux integration suite.

## Performance results

All times end at the first successful application response. Restores below use disk snapshots. The p95 budget is `max(5%, 20 ms)`; p95 means 95% of samples finished within that time.

| Environment | Completed checks | Remaining gaps |
| --- | --- | --- |
| Linux QEMU TAP | Default-open startup passed the 100/100/100 baseline bracket. Serial policies and the 32-destination concurrency-eight case passed. The 100-sample off repeat was +1.0% startup and −0.4% restore versus candidate-open, within budget. | The one-destination concurrency repeat was canceled after the initial restore result exceeded budget. Baseline private-TAP restore failed to reach the application, so no old/new restore percentage is available. |
| Linux QEMU slirp | Application and full-memory restore checks passed. | Performance matrix not run. |
| macOS QEMU/HVF | Default-open startup/restore and serial off passed. | Concurrency-eight off is inconclusive: repeat results exceeded budget, but subsequent open controls showed substantial runner variability. |

The −0.4% result is not evidence of a speedup. It compares off with open within the candidate, not with the old release.

Linux measurements used a two-vCPU, 8 GiB nested-KVM runner with QEMU 8.2.2. macOS used an arm64, 36 GiB machine with QEMU 11.0.0/HVF. Each guest had one vCPU and 512 MiB RAM. Baseline and candidate used matching dependencies and the same cached image within each environment. Eight guests oversubscribed the Linux runner; absolute timings are not a production service-level target.

Full-memory restore on QEMU 11/macOS HVF hit the same `cpu_pre_load` assertion on both the candidate and unchanged baseline. Use disk snapshots on that tested setup; this change does not fix the existing limitation.

## Reproducing the checks

Use the existing `scripts/benchmark-network-policy.py` with a disposable cached image configured to run `tests/e2e/assets/qemu-policy-init.sh` and `qemu-policy-app.py`. Supply its VMConfig through `--vm-config`; use `--restore` for disk restore measurements. Image preparation is outside the timer.

Run baseline-open 100 samples, candidate-open 100, then baseline-open 100 on the same idle runner. Compare startup and restore separately. Sample candidate policies at concurrency one and eight with 24 samples; repeat with 100 when over budget. Keep environments separate and do not average away runner drift.

The opt-in application suite is `tests/e2e/test_qemu_network_policy.py`, configured through `SMOLVM_QEMU_POLICY_CONFIG`. The packet checks are in `tests/e2e/test_network_policy.py`. Privileged checks belong on disposable machines.

## Archived evidence

Raw samples, test logs, environment hashes, and the detailed historical methodology are preserved in the [pre-cleanup evidence snapshot](https://github.com/CelestoAI/SmolVM/tree/242bf792587a55f1b975fde364c9941ae4072372/docs/deep-dive/evidence/qemu-network-policy). The [downloadable source snapshot](https://github.com/CelestoAI/SmolVM/archive/242bf792587a55f1b975fde364c9941ae4072372.zip) includes that evidence directory; it is not an evidence-only attachment.

Generated artifacts are intentionally absent from the current source tree. Superseded runs in the historical archive do not count toward release clearance. Temporary benchmark machines and disks were deleted. No deployment, package publication, or guest-image release was performed.
