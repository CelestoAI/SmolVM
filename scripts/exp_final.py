"""Final before/after: consolidate the winning levers.

BEFORE: the actual out-of-box defaults
  - Firecracker + SSH (Linux default backend, default Alpine image)
  - QEMU + SSH (default image)
AFTER: best stack on QEMU
  - QEMU + vsock + trimmed boot args (needs python3 in image)

5 timed runs each after 1 untimed warm-up. Headline = TOTAL->interact.
"""

from __future__ import annotations

import platform
import statistics as st
import time

from smolvm import SmolVM
from smolvm.facade import _build_auto_config
from smolvm.images.builder import ImageBuilder
from smolvm.images.published import BASE_KERNELS, _kernel_format_for_vmm
from smolvm.runtime.boot_profiles import (
    KernelBootProfile,
    get_boot_profile_spec,
    to_published_arch,
)
from smolvm.types import VMConfig

ARCH = platform.machine()
PROF = KernelBootProfile.MICROVM_DIRECT
N = 5
_seq = 0


def build_py_alpine():
    b = ImageBuilder()
    kurl = BASE_KERNELS[to_published_arch(ARCH)].url_for(_kernel_format_for_vmm("qemu"))
    k, r = b.build_alpine_ssh(name="alpine-py-test", ssh_password="smolvm",
                              rootfs_size_mb=512, kernel_profile=PROF, kernel_url=kurl)
    return str(k), str(r)


def cycle(make_vm) -> dict:
    rec = {}
    vm = None
    try:
        t0 = time.perf_counter(); vm = make_vm(); rec["create"] = time.perf_counter() - t0
        t0 = time.perf_counter(); vm.start(); rec["launch"] = time.perf_counter() - t0
        t0 = time.perf_counter(); vm.run("echo hello"); rec["first"] = time.perf_counter() - t0
        warm = []
        for _ in range(5):
            t0 = time.perf_counter(); vm.run("echo hello"); warm.append(time.perf_counter() - t0)
        rec["warm"] = sum(warm) / len(warm)
    finally:
        if vm is not None:
            try: vm.stop(); vm.delete()
            except Exception: pass
    return rec


def bench(label, make_vm) -> dict:
    print(f"\n== {label}: warm-up ==", flush=True)
    try: cycle(make_vm)
    except Exception as e: print("  warmup err:", e, flush=True)
    runs = []
    errors = []
    for i in range(N):
        try:
            r = cycle(make_vm); runs.append(r)
            print(f"  {i+1}/{N} create={r['create']*1000:.0f} launch={r['launch']*1000:.0f} "
                  f"first={r['first']*1000:.0f} warm={r['warm']*1000:.1f}", flush=True)
        except Exception as e:
            errors.append(str(e))
            print(f"  {i+1}/{N} ERROR {e}", flush=True)
    if not runs:
        print(f"  [{label}] all {N} runs failed", flush=True)
        return {"label": label, "n": 0, "errors": errors,
                "create": None, "launch": None, "first": None,
                "warm": None, "total": None}
    def m(k): return st.mean(x[k] for x in runs) * 1000
    total = m("create") + m("launch") + m("first")
    return {"label": label, "create": m("create"), "launch": m("launch"),
            "first": m("first"), "warm": m("warm"), "total": total,
            "n": len(runs), "errors": errors}


def main():
    global _seq
    print("Building python3 Alpine image for the AFTER cell...", flush=True)
    pk, pr = build_py_alpine()
    ba_default = get_boot_profile_spec(PROF).base_boot_args_for_backend("qemu", ARCH)
    # The default profile already carries tsc=reliable/no_timer_check/quiet
    # (shipped in Q3); only append the delta this experiment tests (acpi=off).
    _present = set(ba_default.split())
    ba_trim = ba_default + "".join(
        f" {flag}" for flag in ("acpi=off",) if flag not in _present
    )

    def mk_default(backend):
        def f():
            global _seq; _seq += 1
            cfg, key = _build_auto_config(vm_name=f"fin-{backend}-{_seq}", os="alpine", backend=backend)
            return SmolVM(config=cfg, ssh_key_path=key)
        return f

    def mk_after():
        global _seq; _seq += 1
        cfg = VMConfig(vm_id=f"fin-after-{_seq}", vcpu_count=1, memory=512,
                       kernel_path=pk, rootfs_path=pr, boot_args=ba_trim, backend="qemu")
        return SmolVM(config=cfg, comm_channel="vsock")

    res = []
    res.append(bench("BEFORE: Firecracker + SSH (Linux default)", mk_default("firecracker")))
    res.append(bench("BEFORE: QEMU + SSH (default image)", mk_default("qemu")))
    res.append(bench("AFTER:  QEMU + vsock + trimmed boot", mk_after))

    print("\n\n=== FINAL BEFORE/AFTER (ms, mean) ===", flush=True)
    h = f"{'configuration':<44}{'create':>8}{'launch':>8}{'first':>8}{'TOTAL':>8}{'warm':>7}"
    print(h); print("-" * len(h))
    for r in res:
        if not r["n"]:
            print(f"{r['label']:<44}  (all runs failed)")
            continue
        print(f"{r['label']:<44}{r['create']:>8.0f}{r['launch']:>8.0f}{r['first']:>8.0f}"
              f"{r['total']:>8.0f}{r['warm']:>7.1f}")
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
