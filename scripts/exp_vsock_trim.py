"""Two experiments on QEMU, same python3-equipped Alpine image:

  EXP A: comm channel — SSH vs vsock (time to interact)
  EXP B: boot cmdline — default vs trimmed device probing (kernel time + total)

All on one image so only the variable under test changes. 4 timed runs each
after 1 untimed warm-up. Reports per-phase means in ms and the kernel's last
printk timestamp (in-kernel boot seconds) from each VM's log.
"""

from __future__ import annotations

import platform
import re
import time

from smolvm import SmolVM
from smolvm.images.builder import ImageBuilder
from smolvm.images.published import BASE_KERNELS, _kernel_format_for_vmm
from smolvm.runtime.boot_profiles import (
    KernelBootProfile,
    get_boot_profile_spec,
    to_published_arch,
)
from smolvm.types import VMConfig
from smolvm.vm import resolve_data_dir

PROF = KernelBootProfile.MICROVM_DIRECT
ARCH = platform.machine()
CMD = "echo hello"
N = 4
_seq = 0


def build_image():
    b = ImageBuilder()
    kurl = BASE_KERNELS[to_published_arch(ARCH)].url_for(_kernel_format_for_vmm("qemu"))
    k, r = b.build_alpine_ssh(
        name="alpine-py-test", ssh_password="smolvm", rootfs_size_mb=512,
        kernel_profile=PROF, kernel_url=kurl,
    )
    return str(k), str(r)


DEFAULT_ARGS = get_boot_profile_spec(PROF).base_boot_args_for_backend("qemu", ARCH)
# The default profile already carries tsc=reliable/no_timer_check/quiet (Q3);
# append only the flags this experiment adds that aren't already present so we
# don't duplicate tokens and muddy the measurement.
_present = set(DEFAULT_ARGS.split())
TRIMMED_ARGS = DEFAULT_ARGS + "".join(
    f" {flag}" for flag in ("acpi=off", "quiet", "no_timer_check", "tsc=reliable")
    if flag not in _present
)


def kernel_last(name: str):
    log = resolve_data_dir() / f"{name}.log"
    if not log.exists():
        return None
    stamps = re.findall(r"\[\s*(\d+\.\d+)\]", log.read_text(errors="replace"))
    return float(stamps[-1]) if stamps else None


def one(kernel, rootfs, *, channel, boot_args, tag) -> dict:
    global _seq
    _seq += 1
    name = f"exp-{tag}-{_seq}"
    cfg = VMConfig(vm_id=name, vcpu_count=1, memory=512, kernel_path=kernel,
                   rootfs_path=rootfs, boot_args=boot_args, backend="qemu")
    rec = {}
    vm = None
    try:
        t0 = time.perf_counter()
        kw = {"comm_channel": channel}
        if channel == "ssh":
            kw["ssh_password"] = "smolvm"
        vm = SmolVM(config=cfg, **kw)
        rec["create"] = time.perf_counter() - t0
        t0 = time.perf_counter(); vm.start(); rec["launch"] = time.perf_counter() - t0
        t0 = time.perf_counter(); vm.run(CMD); rec["first"] = time.perf_counter() - t0
        warm = []
        for _ in range(5):
            t0 = time.perf_counter(); vm.run(CMD); warm.append(time.perf_counter() - t0)
        rec["warm"] = sum(warm) / len(warm)
        rec["kernel_last"] = kernel_last(name)
    finally:
        if vm is not None:
            try: vm.stop(); vm.delete()
            except Exception: pass
    return rec


def run(label, kernel, rootfs, *, channel, boot_args, tag):
    print(f"\n== {label}: warm-up ==", flush=True)
    try:
        one(kernel, rootfs, channel=channel, boot_args=boot_args, tag=tag)
    except Exception as e:
        print("  warmup err:", e, flush=True)
    runs = []
    for i in range(N):
        r = one(kernel, rootfs, channel=channel, boot_args=boot_args, tag=tag)
        runs.append(r)
        print(f"  {i+1}/{N} create={r['create']*1000:.0f} launch={r['launch']*1000:.0f} "
              f"first={r['first']*1000:.0f} warm={r['warm']*1000:.0f} "
              f"klast={r['kernel_last']}s", flush=True)

    def m(k):
        return sum(x[k] for x in runs) / len(runs) * 1000
    kl = [x["kernel_last"] for x in runs if x["kernel_last"]]
    total = m("create") + m("launch") + m("first")
    print(f"  MEAN create={m('create'):.0f} launch={m('launch'):.0f} first={m('first'):.0f} "
          f"warm={m('warm'):.0f}  TOTAL->interact={total:.0f} ms  "
          f"kernel_last={sum(kl)/len(kl):.2f}s" if kl else "", flush=True)
    return {"label": label, "create": m("create"), "launch": m("launch"),
            "first": m("first"), "warm": m("warm"), "total": total,
            "kernel_last": sum(kl)/len(kl) if kl else None}


def main():
    print("Building python3-equipped Alpine image (shared by all cells)...", flush=True)
    kernel, rootfs = build_image()
    print("default boot args:", DEFAULT_ARGS, flush=True)
    print("trimmed boot args:", TRIMMED_ARGS, flush=True)

    res = []
    # EXP A: channel (default boot args)
    res.append(run("A1 SSH   (default boot)", kernel, rootfs, channel="ssh", boot_args=DEFAULT_ARGS, tag="ssh"))
    res.append(run("A2 vsock (default boot)", kernel, rootfs, channel="vsock", boot_args=DEFAULT_ARGS, tag="vsk"))
    # EXP B: boot args (vsock, to isolate boot from SSH-handshake noise)
    res.append(run("B1 vsock + trimmed boot", kernel, rootfs, channel="vsock", boot_args=TRIMMED_ARGS, tag="trim"))

    print("\n\n=== SUMMARY (ms) ===", flush=True)
    h = f"{'cell':<26}{'create':>8}{'launch':>8}{'first':>8}{'TOTAL':>8}{'warm':>7}{'kern_s':>8}"
    print(h); print("-" * len(h))
    for r in res:
        ks = f"{r['kernel_last']:.2f}" if r['kernel_last'] else "-"
        print(f"{r['label']:<26}{r['create']:>8.0f}{r['launch']:>8.0f}{r['first']:>8.0f}"
              f"{r['total']:>8.0f}{r['warm']:>7.0f}{ks:>8}")
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
