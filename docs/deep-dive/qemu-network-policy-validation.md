# QEMU network controls: validation summary

Local applications and shared folders work with the new network controls. Several performance checks passed, but the remaining gaps below mean this change is not fully cleared for production.

## Evidence status

Here, **TAP** means a virtual network interface managed by Linux; **slirp** is
QEMU's built-in networking that needs no such interface. **KVM** (Linux) and
**HVF** (macOS Hypervisor Framework) let QEMU run guest code using hardware acceleration.

The benchmarked implementation is recorded in commit `242bf79`, against the unchanged v0.0.32 baseline (`0ab99c7`). Those measurements predate the port-reservation fix in `aea10eb`; they are not a benchmark of the latest code.

After that fix, the local regression suite passed **2,227 tests**, with 21 skipped and 49 integration cases deselected. Lint passed. The mocked lifecycle tests now explicitly reject attempts to invoke `qemu-img`.

Earlier installed-wheel checks passed:

- Linux QEMU/KVM: 12 TAP application tests, 6 slirp tests, and both Firecracker/QEMU packet-contract cases.
- Linux full-memory restore: TAP off, TAP address restrictions, and slirp off.
- macOS QEMU/HVF: 6 application tests; Linux-only cases skipped.

These checks covered HTTP, file transfers, WebSockets, SDK operations, shared folders, restart, resume, restore, and failed firewall installation/retry. The new port-reservation behavior has local regression coverage but has not been rerun in the real Linux integration suite.

## Performance results

All times end at the first successful application response. Restores below use disk snapshots. **p95** means 95% of samples finished within that time. The allowed increase is the larger of 20 ms or 5% of the reference p95 for the same measurement and environment.

**Baseline-open** is v0.0.32 with unrestricted networking; **candidate-open** is the proposed code with the same setting. Default-open comparisons use baseline-open as the reference. Policy-cost comparisons use candidate-open. A **baseline bracket** runs the baseline before and after the candidate to detect machine variability. **Concurrency-eight** means eight sandboxes run together; **serial** means one at a time.

| Environment | Completed checks | Remaining gaps |
| --- | --- | --- |
| Linux QEMU TAP | Default-open startup passed the 100/100/100 baseline bracket. Serial policies and the 32-destination concurrency-eight case passed. The 100-sample off repeat was +1.0% startup and −0.4% restore versus candidate-open, within budget. | The one-destination concurrency repeat was canceled after the initial restore result exceeded budget. Baseline private-TAP restore failed to reach the application, so no old/new restore percentage is available. |
| Linux QEMU slirp | Application and full-memory restore checks passed. | Performance matrix not run. |
| macOS QEMU/HVF | Default-open startup/restore and serial off passed. | Concurrency-eight off is inconclusive: repeat results exceeded budget, but subsequent open controls showed substantial runner variability. |

The −0.4% result is not evidence of a speedup. It compares off with open within the candidate, not with the old release.

Linux measurements used a two-vCPU, 8 GiB nested-KVM runner with QEMU 8.2.2. macOS used an arm64, 36 GiB machine with QEMU 11.0.0/HVF. Each guest had one vCPU and 512 MiB RAM. Baseline and candidate used matching dependencies and the same cached image within each environment. Eight guests oversubscribed the Linux runner; absolute timings are not a production service-level target.

Full-memory restore on QEMU 11/macOS HVF hit the same `cpu_pre_load` assertion on both the candidate and unchanged baseline. Use disk snapshots on that tested setup; this change does not fix the existing limitation.

## Reproducing the checks

Use an idle disposable Linux machine with QEMU/KVM, Docker, `nft`, and passwordless
sudo configured for SmolVM networking. On macOS, use QEMU/HVF and a running Docker
daemon; the recipe selects slirp instead of TAP. Run from this repository's root
with `uv` installed. Do not run privileged packet tests on production machines.

First build a disposable cached image. This uses the current checkout only for
preparation, outside all measurements. Both guest files come from
`tests/e2e/assets/`; they are installed as `/policy-init` and `/qemu-policy-app.py`
inside the image. The generated `vm-config.json` contains absolute image/kernel
paths and is reused unchanged by both versions.

```bash
export POLICY_BENCH_DIR="$(mktemp -d /tmp/qemu-policy-bench.XXXXXX)"
uv run python - <<'PY'
import os
import sys
from pathlib import Path
from smolvm import DockerRootfsBuilder, VMConfig
from smolvm.images import DirectKernelBoot

directory = Path(os.environ["POLICY_BENCH_DIR"])
assets = Path("tests/e2e/assets").resolve()
image = DockerRootfsBuilder(
    name="qemu-policy-benchmark",
    cache_dir=directory / "images",
    dockerfile="""FROM alpine:3.21
RUN apk add --no-cache python3 iproute2
COPY policy-init /policy-init
COPY qemu-policy-app.py /qemu-policy-app.py
RUN chmod +x /policy-init
""",
    context={
        "policy-init": assets / "qemu-policy-init.sh",
        "qemu-policy-app.py": assets / "qemu-policy-app.py",
    },
).build_boot_image(backend="qemu", boot=DirectKernelBoot(init="/policy-init"))
config = VMConfig(
    vm_id="policy-template", backend="qemu", guest_os="alpine",
    qemu_network="tap" if sys.platform == "linux" else "slirp",
    memory=512, vcpu_count=1, rootfs_path=image.rootfs_path,
    rootfs_format=image.rootfs_format, kernel_path=image.kernel_path,
    boot_args=image.render_boot_args(backend="qemu", arch=image.arch),
)
(directory / "vm-config.json").write_text(config.model_dump_json(indent=2))
print(directory / "vm-config.json")
PY
# Prints /tmp/qemu-policy-bench.<generated suffix>/vm-config.json
```

Prepare separate installed-wheel environments with the same locked dependencies.
Keep this checkout's benchmark script for both versions.

```bash
uv export --no-dev --no-emit-project --format requirements.txt > "$POLICY_BENCH_DIR/dependencies.txt"
uv build --wheel --out-dir "$POLICY_BENCH_DIR/wheels"
for version in baseline candidate; do
  uv venv --python .venv/bin/python "$POLICY_BENCH_DIR/$version"
  uv pip install --python "$POLICY_BENCH_DIR/$version/bin/python" -r "$POLICY_BENCH_DIR/dependencies.txt"
done
uv pip install --python "$POLICY_BENCH_DIR/baseline/bin/python" --no-deps smolvm==0.0.32
uv pip install --python "$POLICY_BENCH_DIR/candidate/bin/python" --no-deps "$POLICY_BENCH_DIR"/wheels/*.whl
```

Run the startup bracket. Each command writes its samples to the named JSONL file;
the data directories are disposable VM storage, separate from the shared image.

```bash
for run in baseline-before candidate baseline-after; do
  version=baseline
  [ "$run" != candidate ] || version=candidate
  "$POLICY_BENCH_DIR/$version/bin/python" scripts/benchmark-network-policy.py \
    --vm-config "$POLICY_BENCH_DIR/vm-config.json" --mode open --samples 100 \
    --data-dir "$POLICY_BENCH_DIR/data-$run" --output "$POLICY_BENCH_DIR/$run.jsonl"
done
```

To include disk restore measurements, add `--restore`. For example, measure off
with eight simultaneous sandboxes (compare against candidate-open with identical
sample count, concurrency, and restore flags):

```bash
"$POLICY_BENCH_DIR/candidate/bin/python" scripts/benchmark-network-policy.py \
  --vm-config "$POLICY_BENCH_DIR/vm-config.json" --mode off --samples 24 \
  --concurrency 8 --restore --data-dir "$POLICY_BENCH_DIR/data-off" \
  --output "$POLICY_BENCH_DIR/off-c8.jsonl"
```

For Linux TAP address restrictions, replace `--mode off` with
`--mode restricted --allow 203.0.113.7/32`; repeat `--allow` for more destinations.
These are filtering-cost measurements, not reachability checks of that example
address. On Linux, change the generated JSON's `qemu_network` to `slirp` to test
that path separately. Slirp workers keep distinct port claims through startup
and restore without serializing VM starts. External bind conflicts get at most
two retries, included in latency and reported as `forward_bind_retries`.

Run baseline-open 100 samples, candidate-open 100, then baseline-open 100 on the same idle runner. Compare startup and restore separately. Sample candidate policies at concurrency one and eight with 24 samples; repeat with 100 when over budget. Keep environments separate and do not average away runner drift.

The opt-in application suite is `tests/e2e/test_qemu_network_policy.py`, configured through `SMOLVM_QEMU_POLICY_CONFIG`. The packet checks are in `tests/e2e/test_network_policy.py`. Privileged checks belong on disposable machines.

## Archived evidence

Raw samples, test logs, environment hashes, and the detailed historical methodology are preserved in the [pre-cleanup evidence snapshot](https://github.com/CelestoAI/SmolVM/tree/242bf792587a55f1b975fde364c9941ae4072372/docs/deep-dive/evidence/qemu-network-policy). The [downloadable source snapshot](https://github.com/CelestoAI/SmolVM/archive/242bf792587a55f1b975fde364c9941ae4072372.zip) includes that evidence directory; it is not an evidence-only attachment.

Generated artifacts are intentionally absent from the current source tree. Superseded runs in the historical archive do not count toward release clearance. Temporary benchmark machines and disks were deleted. No deployment, package publication, or guest-image release was performed.
