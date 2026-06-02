"""Experiment C: trim userspace on the SSH critical path.

Finding 3/4 showed that for the SSH channel, /init regenerates SSH host keys on
every boot (~190 ms) because the Dockerfile deletes them. This experiment builds
a variant image that bakes host keys at build time and skips keygen in /init,
then measures SSH-path time-to-interact vs the baseline.

All on QEMU, same Alpine+python3 base, default boot args. Ground-truth boot time
comes from the guest's SMOLVM_TS uptime markers (not 'quiet'-suppressed printk).
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
N = 4
_seq = 0

# Baseline Dockerfile = build_alpine_ssh recipe (deletes host keys -> per-boot keygen).
BASELINE_DOCKERFILE = """
FROM alpine:3.19
ARG SSH_PASSWORD
RUN apk add --no-cache openssh iproute2 curl bash python3
RUN rm -f /etc/ssh/ssh_host_* && \\
    echo "root:${SSH_PASSWORD}" | chpasswd && \\
    sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config && \\
    sed -i 's/#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
COPY init /init
RUN chmod +x /init
"""

# Variant Dockerfile = bake host keys at build time (ssh-keygen -A once).
BAKED_DOCKERFILE = """
FROM alpine:3.19
ARG SSH_PASSWORD
RUN apk add --no-cache openssh iproute2 curl bash python3
RUN rm -f /etc/ssh/ssh_host_* && ssh-keygen -A && \\
    echo "root:${SSH_PASSWORD}" | chpasswd && \\
    sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config && \\
    sed -i 's/#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
COPY init /init
RUN chmod +x /init
"""


def build(name: str, dockerfile: str):
    b = ImageBuilder()
    image_dir = b.cache_dir / name
    kernel_path = image_dir / "vmlinux.bin"
    rootfs_path = image_dir / "rootfs.ext4"
    kurl = BASE_KERNELS[to_published_arch(ARCH)].url_for(_kernel_format_for_vmm("qemu"))
    init_script = b._default_init_script()
    image_dir.mkdir(parents=True, exist_ok=True)
    b._do_build(
        name, dockerfile, init_script, image_dir, kernel_path, rootfs_path,
        rootfs_size_mb=512, build_args={"SSH_PASSWORD": "smolvm"}, kernel_url=kurl,
    )
    return str(kernel_path), str(rootfs_path)


def markers(name: str) -> dict:
    """Parse SMOLVM_TS uptime markers; return stage -> uptime_s."""
    log = resolve_data_dir() / f"{name}.log"
    out = {}
    if log.exists():
        for m in re.finditer(r"SMOLVM_TS stage=(\S+).*?uptime_s=([\d.]+)", log.read_text(errors="replace")):
            out[m.group(1)] = float(m.group(2))
    return out


def one(kernel, rootfs, tag) -> dict:
    global _seq
    _seq += 1
    name = f"ec-{tag}-{_seq}"
    ba = get_boot_profile_spec(PROF).base_boot_args_for_backend("qemu", ARCH)
    cfg = VMConfig(vm_id=name, vcpu_count=1, memory=512, kernel_path=kernel,
                   rootfs_path=rootfs, boot_args=ba, backend="qemu")
    rec = {}
    vm = None
    try:
        t0 = time.perf_counter()
        vm = SmolVM(config=cfg, ssh_password="smolvm", comm_channel="ssh")
        rec["create"] = time.perf_counter() - t0
        t0 = time.perf_counter(); vm.start(); rec["launch"] = time.perf_counter() - t0
        t0 = time.perf_counter(); vm.run("echo hello"); rec["first"] = time.perf_counter() - t0
        time.sleep(0.3)  # let init flush trailing markers to the log
        mk = markers(name)
        rec["keygen_ms"] = (
            (mk["ssh-hostkey-check-done"] - mk["ssh-hostkey-check-start"]) * 1000
            if "ssh-hostkey-check-done" in mk and "ssh-hostkey-check-start" in mk else None
        )
        rec["sshd_uptime"] = mk.get("sshd-invoked")
    finally:
        if vm is not None:
            try: vm.stop(); vm.delete()
            except Exception: pass
    return rec


def run(label, kernel, rootfs, tag):
    print(f"\n== {label}: warm-up ==", flush=True)
    try: one(kernel, rootfs, tag)
    except Exception as e: print("  warmup err:", e, flush=True)
    runs = []
    for i in range(N):
        try:
            r = one(kernel, rootfs, tag)
        except Exception as e:
            print(f"  {i+1}/{N} ERROR {e}", flush=True)
            continue
        runs.append(r)
        kg = f"{r['keygen_ms']:.0f}" if r['keygen_ms'] is not None else "-"
        su = f"{r['sshd_uptime']:.3f}" if r['sshd_uptime'] is not None else "-"
        print(f"  {i+1}/{N} create={r['create']*1000:.0f} launch={r['launch']*1000:.0f} "
              f"first={r['first']*1000:.0f}  keygen={kg}ms sshd_up={su}s", flush=True)

    if not runs:
        raise RuntimeError(f"{label}: all {N} runs failed; no samples collected")

    def m(k): return sum(x[k] for x in runs) / len(runs) * 1000
    total = m("create") + m("launch") + m("first")
    kgs = [x["keygen_ms"] for x in runs if x["keygen_ms"] is not None]
    sus = [x["sshd_uptime"] for x in runs if x["sshd_uptime"] is not None]
    print(f"  MEAN create={m('create'):.0f} launch={m('launch'):.0f} first={m('first'):.0f} "
          f"TOTAL->interact={total:.0f} ms  "
          f"keygen={sum(kgs)/len(kgs):.0f}ms " if kgs else "",
          f"sshd_uptime={sum(sus)/len(sus):.3f}s" if sus else "", flush=True)
    return {"label": label, "create": m("create"), "launch": m("launch"),
            "first": m("first"), "total": total,
            "keygen": sum(kgs)/len(kgs) if kgs else None,
            "sshd_uptime": sum(sus)/len(sus) if sus else None}


def main():
    print("Building baseline (per-boot keygen) image...", flush=True)
    bk, br = build("ec-baseline", BASELINE_DOCKERFILE)
    print("Building baked-host-keys image...", flush=True)
    xk, xr = build("ec-baked", BAKED_DOCKERFILE)

    res = []
    res.append(run("C0 baseline (keygen each boot)", bk, br, "base"))
    res.append(run("C1 baked host keys", xk, xr, "baked"))

    print("\n\n=== EXPERIMENT C SUMMARY (ms) ===", flush=True)
    h = f"{'cell':<32}{'create':>8}{'launch':>8}{'first':>8}{'TOTAL':>8}{'keygen':>8}"
    print(h); print("-" * len(h))
    for r in res:
        kg = f"{r['keygen']:.0f}" if r['keygen'] is not None else "-"
        print(f"{r['label']:<32}{r['create']:>8.0f}{r['launch']:>8.0f}{r['first']:>8.0f}{r['total']:>8.0f}{kg:>8}")
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
