"""Break the ~1.5s 'time to interact' into its real sub-components.

For each backend, one VM:
  t_create   : SmolVM(config) constructor
  t_launch   : .start() (hypervisor process up; does NOT wait for guest)
  t_tcp_open : from launch-return until the guest's SSH port (22) accepts a
               TCP connection  = guest kernel boot + init + sshd listening
  t_ssh_auth : paramiko handshake + key auth on the now-open port
  t_cmd      : actually running `echo hello`

Also greps the guest kernel log for the last printk timestamp, which tells us
how much of t_tcp_open is kernel boot vs userspace (init + sshd).
"""

from __future__ import annotations

import re
import socket
import time
from pathlib import Path

from smolvm import SmolVM
from smolvm.facade import _build_auto_config
from smolvm.ssh import SSHClient
from smolvm.vm import resolve_data_dir

OS = "alpine"
CMD = "echo hello"


def tcp_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


_seq = 0


def profile(backend: str) -> dict:
    global _seq
    _seq += 1
    name = f"prof-{backend}-{_seq}"
    r: dict = {"backend": backend}

    t0 = time.perf_counter()
    config, key = _build_auto_config(vm_name=name, os=OS, backend=backend)
    vm = SmolVM(config=config, ssh_key_path=key)
    r["create"] = time.perf_counter() - t0

    client = None
    try:
        t0 = time.perf_counter()
        vm.start()
        r["launch"] = time.perf_counter() - t0

        # Use the SDK's own endpoint resolution (QEMU = forwarded 127.0.0.1:port,
        # Firecracker = TAP guest_ip:22).
        host, port = vm._ssh_endpoints()[0]
        r["endpoint"] = f"{host}:{port}"

        # Phase A: spin on TCP connect until sshd is listening.
        t0 = time.perf_counter()
        deadline = t0 + 30
        while time.perf_counter() < deadline:
            if tcp_open(host, port):
                break
            time.sleep(0.02)
        r["tcp_open"] = time.perf_counter() - t0

        # Phase B: paramiko handshake + auth on the open port.
        t0 = time.perf_counter()
        client = SSHClient(host=host, port=port, user="root", key_path=key)
        client.wait_for_ssh(timeout=30)
        r["ssh_auth"] = time.perf_counter() - t0

        # Phase C: run the command over the established connection.
        t0 = time.perf_counter()
        client.run(CMD)
        r["cmd"] = time.perf_counter() - t0

        # Kernel log: last "[ TIMESTAMP ]" printk = seconds of in-kernel time.
        log_path = resolve_data_dir() / f"{name}.log"
        kernel_last = None
        n_lines = 0
        if log_path.exists():
            text = log_path.read_text(errors="replace")
            n_lines = text.count("\n")
            stamps = re.findall(r"\[\s*(\d+\.\d+)\]", text)
            if stamps:
                kernel_last = float(stamps[-1])
        r["kernel_last_printk_s"] = kernel_last
        r["log_lines"] = n_lines
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as e:
                print(f"  cleanup: client.close failed: {e}", flush=True)
        if vm is not None:
            try:
                vm.stop()
                vm.delete()
            except Exception as e:
                print(f"  cleanup: vm stop/delete failed: {e}", flush=True)
    return r


def main():
    for backend in ("qemu", "firecracker"):
        print(f"== {backend}: warm-up ==", flush=True)
        try:
            profile(backend)  # warm caches
        except Exception as e:
            print("warmup err", e, flush=True)

        print(f"== {backend}: profiling 3 runs ==", flush=True)
        runs = []
        for i in range(3):
            r = profile(backend)
            runs.append(r)
            print(
                f"  run {i + 1}: [{r['endpoint']}] create={r['create']*1000:.0f}  launch={r['launch']*1000:.0f}  "
                f"tcp_open={r['tcp_open']*1000:.0f}  ssh_auth={r['ssh_auth']*1000:.0f}  "
                f"cmd={r['cmd']*1000:.0f}  | kernel_last={r['kernel_last_printk_s']}s  "
                f"loglines={r['log_lines']}",
                flush=True,
            )
        # mean
        def mean(k):
            return sum(x[k] for x in runs) / len(runs) * 1000
        print(
            f"  MEAN  create={mean('create'):.0f}  launch={mean('launch'):.0f}  "
            f"tcp_open={mean('tcp_open'):.0f}  ssh_auth={mean('ssh_auth'):.0f}  "
            f"cmd={mean('cmd'):.0f}  (ms)",
            flush=True,
        )
        kl = [x["kernel_last_printk_s"] for x in runs if x["kernel_last_printk_s"]]
        if kl:
            print(f"  kernel last printk (mean): {sum(kl)/len(kl):.2f}s", flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
