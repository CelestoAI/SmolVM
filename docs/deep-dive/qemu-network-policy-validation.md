# QEMU network controls: release checks

QEMU application speed is the release gate. These changes are not cleared for production until the relevant environment passes both the application checks and its performance budget.

## Scope

The candidate adds Linux QEMU TAP off/IPv4-address restrictions and portable macOS/Linux slirp off. It preserves default-open behavior, existing shared folders, native application forwarding, and saved policy formats. It does not add DNS services, domain filtering, proxies, helper VMs in the product, or checks on each command.

The implementation also fixes private-TAP snapshot restore ordering and a slirp restart preflight bug: QEMU can reuse ports with completed connections, but the old preflight incorrectly treated those ports as occupied. Real Linux testing caught two further lifecycle issues: repair must not recreate a TAP already held open by QEMU, and a process reaped during the existing status probe must not be reported as still running. Both fixes stay in lifecycle reconciliation; neither adds a helper or a policy check to application traffic.

## Evidence status

Baseline: **v0.0.32**, commit `0ab99c7`. Its wheel was built before production code changes. Baseline and candidate must use the same installed dependency versions, image, resources, application, and runner.

| Environment | Application verification | Performance gate |
| --- | --- | --- |
| Linux QEMU TAP / KVM | All 12 installed-wheel application/lifecycle/failure tests passed; full-memory off/CIDR restores passed | Default-open startup and serial policies pass; concurrent off/one-destination repeats in progress. Baseline disk restore does not reach the application |
| Linux QEMU slirp / KVM | 6 installed-wheel tests passed, 6 TAP-only cases skipped; full-memory off restore passed | Pending; baseline needs a documented, verified-dead cleanup retry outside timed regions |
| macOS QEMU / HVF | Source and installed-wheel workflow passed: 6 tests; Linux-only cases skipped | Default-open and serial off pass; concurrency-eight off remains **inconclusive, not cleared** |

The [final Linux packet contract](evidence/qemu-network-policy/linux-functional/packet.xml) passed both Firecracker and QEMU cases on the replacement KVM runner. QEMU checks include direct guest-IP and localhost-translated application replies through restricted, off, failed-replacement, and interface-reuse transitions. The [12 TAP tests](evidence/qemu-network-policy/linux-functional/tap-e2e.xml) include shared folders and SDK transfers under both off and CIDR restrictions; the [slirp tests](evidence/qemu-network-policy/linux-functional/slirp-e2e.xml) passed all 6 applicable cases. The [earlier arm64 namespace check](evidence/qemu-network-policy/linux-packet-contract.txt) is supplementary kernel-rule evidence, not KVM lifecycle performance.

The [final regression suite](evidence/qemu-network-policy/regression.txt) passed **2,222 tests**, with 21 skipped and 49 E2E cases deselected, including all six open/off/restricted Firecracker/QEMU restore-order/failure cases. Ruff and whitespace checks pass. The [final installed-wheel Mac application suite](evidence/qemu-network-policy/macos-hvf-final/macos-installed-e2e-complete.xml) passed 6 tests with 6 Linux-TAP-only cases skipped. An earlier unit run collided with an active disposable test VM's fixed SSH port; the clean regression run above was done after stopping all local test guests.

**Full-memory restore:** Linux/QEMU 8.2.2/KVM passed application and saved-policy checks for [TAP off](evidence/qemu-network-policy/linux-functional/full-tap-off.log), [TAP CIDRs](evidence/qemu-network-policy/linux-functional/full-tap-restricted.log), and [slirp off](evidence/qemu-network-policy/linux-functional/full-slirp-off.log). On macOS, QEMU 11.0.0/HVF aborts in `target/arm/machine.c:1045`, `cpu_pre_load`, with `!cpu->cpreg_vmstate_indexes`. The [final candidate off-mode smoke](evidence/qemu-network-policy/macos-hvf-final/macos-full-candidate.log) failed; the [unchanged v0.0.32 open-mode control](evidence/qemu-network-policy/macos-hvf-final/macos-full-baseline.log) reproduced the assertion. That Mac limitation is not cleared or fixed by this change. All performance measurements below use disk snapshots.

The configured `turing-machine` host was unreachable. After approval to start the existing GCP test VM `darkhorse-vm`, two start attempts on September 9, 2026 failed with `ZONE_RESOURCE_POOL_EXHAUSTED`: `n2-standard-2` capacity was unavailable in `europe-west2-a` (project `bright-benefit-464400-c6`). The instance remained `TERMINATED`.

With separate approval, a temporary `n2-standard-2` runner named `smolvm-qemu-policy-0909-01a084f7` was created in `europe-west4-a`: Ubuntu 24.04, Linux 7.0.0-1011-gcp, QEMU 8.2.2, Python 3.12.3, nested KVM, 8 GiB RAM, and a 40 GB balanced persistent disk. A long suspension of the local controller prevented complete evidence retrieval before its three-hour deletion backstop fired. Both the instance and boot disk were confirmed deleted. Its saved application results remain valid, but the incomplete performance run is not a release gate.

An identically sized replacement, `smolvm-qemu-policy-0909-01a084f7-r2`, was created at 16:27 UTC on September 9 with a two-hour deletion backstop. It has no service account; its boot disk is auto-deleted. Each completed case is copied locally before the next starts, and local sleep is temporarily inhibited during measurement. This runner will also be explicitly deleted after collection. No production VM was used or resized.

The [unchanged v0.0.32 TAP smoke](evidence/qemu-network-policy/linux-baseline-restore-failure.txt) reached the application on initial start, but timed out after snapshot/delete/restore. Therefore the TAP startup comparison uses three startup-only 100-sample runs; candidate restore latency is collected separately. A percentage restore comparison against this broken baseline would be misleading. The original baseline is not patched or given network preparation outside the timer.

Initial Mac application smoke measurements are not release evidence: they were interleaved with debugging and unit tests. Only clean completed runs count. A skipped test or a QEMU process becoming ready is not evidence of usable application access.

### Linux TAP performance results

Runner: the replacement GCP VM described above, with two vCPUs and nested KVM. Each sandbox has one vCPU and 512 MiB RAM. Eight concurrent sandboxes heavily oversubscribe this small runner; these absolute latencies are not a production service-level target. Compare modes only at the same concurrency. Values are nearest-rank p95 milliseconds.

| Run, in execution order | Samples | Concurrency | Startup p95 | Disk restore p95 |
| --- | ---: | ---: | ---: | ---: |
| v0.0.32 before, startup only | 100 | 1 | 2,275.6 | Unavailable |
| Candidate open, startup only | 100 | 1 | 2,275.0 | — |
| v0.0.32 after, startup only | 100 | 1 | 2,275.2 | Unavailable |
| Candidate open | 100 | 1 | 2,277.5 | 2,985.1 |
| Candidate open | 24 | 8 | 13,658.2 | 28,543.5 |
| Candidate off | 24 | 1 | 2,276.3 | 2,817.5 |
| Candidate off | 24 | 8 | 13,745.4 | 30,031.0 |
| Candidate, one destination | 24 | 1 | 2,276.5 | 2,871.4 |
| Candidate, one destination | 24 | 8 | 13,741.8 | 30,130.0 |
| Candidate, 32 destinations | 24 | 1 | 2,276.7 | 2,871.2 |
| Candidate, 32 destinations | 24 | 8 | 13,550.8 | 28,684.1 |

Default-open startup passes both baseline comparisons. All serial policy cases and the 32-destination concurrency case are within budget. Concurrent off restore is +5.2% and one-destination restore is +5.6% versus open, so both require the agreed 100-sample repeat; that repeat is in progress, not yet a pass. Baseline private-TAP restore remains unavailable for the reason above.

The [summary and raw samples](evidence/qemu-network-policy/linux-tap/summary.json) and [environment, matching packages, and artifact hashes](evidence/qemu-network-policy/linux-tap/environment.json) are retained together.

### Final-wheel macOS performance results

Runner: macOS 26.5.1, arm64, 36 GiB RAM, QEMU 11.0.0/HVF, Python 3.14. Each sandbox used one vCPU, 512 MiB RAM, and the same cached Ubuntu test application and kernel. Baseline and candidate production dependencies matched. No other task-generated VM or test workload ran during these measurements.

All **672 recorded startup/restore pairs** completed successfully. Each duration ends at the first successful HTTP application response. These are disk restores, not full-memory restores. Values below are nearest-rank p95, in milliseconds, rounded only for display. Final candidate wheel SHA-256: `9445139fa003eb9a70826bd064ecd6f7281320a7f1300f641ea58b35398640a2`.

| Run, in execution order | Samples | Concurrency | Startup p95 | Disk restore p95 |
| --- | ---: | ---: | ---: | ---: |
| v0.0.32 before | 100 | 1 | 1,178.4 | 1,396.3 |
| Candidate open | 100 | 1 | 1,194.3 | 1,405.8 |
| v0.0.32 after | 100 | 1 | 1,193.2 | 1,389.3 |
| Candidate open | 24 | 8 | 1,651.9 | 3,875.0 |
| Candidate off | 24 | 1 | 1,135.9 | 1,391.8 |
| Candidate off | 24 | 8 | 1,531.3 | 6,140.1 |
| Candidate open, repeat | 100 | 8 | 1,583.9 | 3,658.6 |
| Candidate off, repeat | 100 | 8 | 2,021.1 | 4,100.1 |
| Candidate open, follow-up control | 100 | 8 | 2,485.8 | 5,274.9 |

Default-open passes against both baselines: at most +1.3% startup and +1.2% disk restore. Serial off is also within budget, with both p95 values lower than candidate-open. Lower values are not an optimization claim.

**Concurrency-eight off is not cleared.** The initial restore result was +58.5% versus open, triggering the required 100-sample repeat. The repeat was +27.6% startup and +12.1% restore versus its preceding open run. An immediate 100-sample open control was then slower than off on both metrics, demonstrating substantial runner variability. This does not establish a policy-caused regression, but it also does not establish a pass. Repeat this gate on a stable macOS/HVF runner before releasing that increment; do not average away the failed comparisons.

The [final summary and adjacent raw JSONL files](evidence/qemu-network-policy/macos-hvf-final/summary.json) and [environment and artifact hashes](evidence/qemu-network-policy/macos-hvf-final/environment.json) are retained together. The [earlier 572-pair matrix](evidence/qemu-network-policy/macos-hvf/summary.json) predates the final lifecycle fixes and does not clear the final wheel. A separate 372-pair matrix was affected by local suspension and is excluded. The final matrix above ran with sleep inhibited.

## Repeatable application fixture

The test-only application in `tests/e2e/assets/qemu-policy-app.py` answers HTTP, echoes uploaded bytes, handles a WebSocket exchange, and probes TCP/UDP destinations. `qemu-policy-init.sh` starts it without SSH or a guest agent. These files are never included in a published guest image.

Use a disposable **copy** of a cached Ubuntu ext4 image containing Python 3 and its matching kernel. Keep the original image untouched. The existing `/init` must remain available for the separate shared-folder/SDK tests. From the repository root:

```bash
mkdir /tmp/qemu-policy-image
cp /path/to/cached/rootfs.ext4 /tmp/qemu-policy-image/app.ext4
debugfs -w -R 'write tests/e2e/assets/qemu-policy-init.sh /policy-init' /tmp/qemu-policy-image/app.ext4
debugfs -w -R 'set_inode_field /policy-init mode 0100755' /tmp/qemu-policy-image/app.ext4
debugfs -w -R 'write tests/e2e/assets/qemu-policy-app.py /qemu-policy-app.py' /tmp/qemu-policy-image/app.ext4
qemu-img convert -f raw -O qcow2 /tmp/qemu-policy-image/app.ext4 /tmp/qemu-policy-image/app.qcow2
```

Supply a VMConfig JSON pointing at the copy and matching kernel. Choose `"qemu_network": "tap"` on the production-like Linux runner or `"slirp"` for the portable case; do not change it between baseline and candidate. Use `"backend": "qemu"`, `"guest_os": "ubuntu"`, `"comm_channel": "ssh"`, `"rootfs_format": "qcow2"`, and `init=/policy-init` in `boot_args`. Set the same CPU/memory limits for every case. The SSH setting reserves a port but the application-only init does not run SSH.

Run the public API suite against an **installed candidate wheel**, from outside the checkout:

```bash
SMOLVM_QEMU_POLICY_CONFIG=/tmp/qemu-policy-image/config.json \
  /path/to/candidate/bin/python -m pytest /path/to/SmolVM/tests/e2e/test_qemu_network_policy.py \
  -m e2e --override-ini addopts='' -v
```

This covers `from_image(state_manager=...)`, direct VMConfig, async start/resume, pause, restart, repair, disk restore, launch-time/dynamic forwarding, HTTP, a 1 MiB application upload/download, WebSockets, SDK files, and read-only-overlay/writable shared folders. Dynamic exposures are recreated after pause, stop, and restore, as required by their existing lifetime. The no-agent fixture explicitly requests crash-consistent disk snapshots (`flush_policy="skip"`); it does not change the normal snapshot flush default.

On Linux TAP, the same suite reuses the existing controlled namespace lab to check allowed/denied TCP/UDP destinations and actual nft rejection/retry at create, start, resume, live repair, and restore. Run `tests/e2e/test_network_policy.py -k firewall_packet_contract` as well: it exercises both Firecracker rules and QEMU reply rules, including IPv6, existing isolation, stale outbound state, interface reuse, and atomic failure retention. These privileged tests belong on disposable machines.

## Performance gates

Use the corrected batch sequencing from #498: finish every start in a batch, snapshot all, dispose of **all** originals, then restore the batch. This avoids concurrent restore/address-reservation races. No image download or build is timed. One warmup is unrecorded.

```bash
/path/to/baseline/bin/python /path/to/SmolVM/scripts/benchmark-network-policy.py \
  --vm-config /tmp/qemu-policy-image/config.json --samples 100 --restore \
  --data-dir /tmp/qemu-policy-run --output /tmp/baseline-before.jsonl
```

Run 100 baseline-open samples, 100 candidate-open samples, then another 100 baseline-open samples, serially on an otherwise idle runner. Time ends at the first HTTP 200 response with the expected body. Startup includes construction and start; restore includes the public `from_snapshot()` call and the first application response. Snapshot preparation and teardown are outside both measurements.

Compare startup and restore p95 separately. A repeatable increase above `max(5% of baseline p95, 20 ms)` blocks that increment. Do not mask runner drift by averaging incompatible baseline runs.

Then run candidate-open and off at concurrency one and eight, using 24 samples each. On Linux TAP add restricted mode with one destination and 32 **nonadjacent** destinations (so CIDR normalization does not collapse the list). Pass repeated `--allow` arguments with `--mode restricted`. Compare each policy case with candidate-open at the same concurrency. Repeat with 100 samples if a material regression appears; the same repeatable-regression budget applies.

Keep raw JSONL, environment/package versions, image hashes, test results, and nearest-rank p95 summaries together. Report Linux TAP, Linux slirp, and macOS HVF separately. Passing Mac measurements never clears the Linux production gate.

No package publication, guest-image release, or production deployment is part of this work.
