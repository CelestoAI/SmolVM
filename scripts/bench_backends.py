"""Benchmark SmolVM QEMU vs Firecracker backends.

Same guest OS (Alpine) on both backends to isolate the hypervisor.
Per backend, over N timed iterations, measures (seconds):
  create     : build config + SmolVM(config=...) (registers VM, materializes rootfs overlay)
  boot       : .start() until the VM is running
  first_cmd  : first vm.run() — includes waiting for SSH = "time to interact"
  warm_cmd   : mean latency of subsequent vm.run() calls in the same VM

Explicit unique VM names (bench-<backend>-<n>) avoid the auto-namer.
A warm-up iteration per backend (untimed) builds/downloads any missing
image artifacts so those costs don't land in the timings.
"""

from __future__ import annotations

import json
import statistics as st
import time
import traceback

from smolvm import SmolVM
from smolvm.facade import _build_auto_config

OS = "alpine"
ITERS = 5
WARM_RUNS = 5
CMD = "echo hello"

_counter = 0


def _unique_name(backend: str) -> str:
    global _counter
    _counter += 1
    return f"bench-{backend}-{_counter}"


def time_one(backend: str) -> dict:
    """One full create->boot->run->stop cycle; returns phase timings (seconds)."""
    rec: dict = {}
    vm = None
    try:
        name = _unique_name(backend)
        t0 = time.perf_counter()
        config, key = _build_auto_config(vm_name=name, os=OS, backend=backend)
        vm = SmolVM(config=config, ssh_key_path=key)
        rec["create"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        vm.start()
        rec["boot"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        vm.run(CMD)
        rec["first_cmd"] = time.perf_counter() - t0

        warm = []
        for _ in range(WARM_RUNS):
            t0 = time.perf_counter()
            vm.run(CMD)
            warm.append(time.perf_counter() - t0)
        rec["warm"] = warm
    finally:
        if vm is not None:
            try:
                vm.stop()
            except Exception:
                pass
            try:
                vm.delete()
            except Exception:
                pass
    return rec


def bench(backend: str) -> dict:
    print(f"== {backend}: warm-up (untimed) ==", flush=True)
    try:
        time_one(backend)
    except Exception as e:
        print(f"  warm-up failed: {e}", flush=True)
        traceback.print_exc()

    create, boot, first, warm = [], [], [], []
    errors = []
    for i in range(ITERS):
        try:
            r = time_one(backend)
            create.append(r["create"])
            boot.append(r["boot"])
            first.append(r["first_cmd"])
            warm.extend(r["warm"])
            print(
                f"  [{backend}] {i + 1}/{ITERS}  "
                f"create={r['create'] * 1000:.0f}ms boot={r['boot'] * 1000:.0f}ms "
                f"first={r['first_cmd'] * 1000:.0f}ms",
                flush=True,
            )
        except Exception as e:
            errors.append(str(e))
            traceback.print_exc()

    def summ(v):
        return None if not v else {
            "n": len(v),
            "min": min(v),
            "mean": st.mean(v),
            "median": st.median(v),
            "stdev": st.stdev(v) if len(v) > 1 else 0.0,
            "max": max(v),
        }

    return {
        "backend": backend,
        "os": OS,
        "create": summ(create),
        "boot": summ(boot),
        "first_cmd": summ(first),
        "warm_cmd": summ(warm),
        "create_to_ready": summ([c + b for c, b in zip(create, boot)]),
        # The honest end-to-end metric: nothing -> command returned.
        "total_to_interact": summ([c + b + f for c, b, f in zip(create, boot, first)]),
        "errors": errors,
    }


def main():
    results = [bench("qemu"), bench("firecracker")]
    with open("/tmp/bench_results.json", "w") as f:
        json.dump(
            {"iters": ITERS, "warm_runs": WARM_RUNS, "cmd": CMD, "results": results},
            f,
            indent=2,
        )

    def ms(s, k="mean"):
        return f"{s[k] * 1000:.0f}" if s else "-"

    print("\n=== RESULTS (ms, mean over timed iters) ===", flush=True)
    cols = (
        f"{'backend':<13}{'create':>9}{'launch':>9}{'first_cmd':>11}"
        f"{'TOTAL->interact':>17}{'warm_cmd':>10}"
    )
    print(cols)
    print("-" * len(cols))
    for r in results:
        print(
            f"{r['backend']:<13}"
            f"{ms(r['create']):>9}"
            f"{ms(r['boot']):>9}"
            f"{ms(r['first_cmd']):>11}"
            f"{ms(r['total_to_interact']):>17}"
            f"{ms(r['warm_cmd']):>10}"
        )
        if r["errors"]:
            print(f"   errors ({len(r['errors'])}): {r['errors'][:2]}")
    print("\n(create=ctor, launch=hypervisor up, first_cmd=wait-for-SSH+run,", flush=True)
    print(" TOTAL->interact = create+launch+first_cmd = nothing -> command returned)", flush=True)
    print("\nwrote /tmp/bench_results.json", flush=True)


if __name__ == "__main__":
    main()
