# Copyright 2026 Celesto AI
# Licensed under the Apache License, Version 2.0
"""Entrypoint that runs inside the libkrun child process.

Invoked as ``python -m smolvm.runtime._libkrun_launcher <config.json>``.
Starts gvproxy for networking on macOS, then blocks on ``krun_start_enter``
for the lifetime of the guest.
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

from smolvm.runtime._libkrun_ffi import KERNEL_FORMAT_RAW, KrunContext, _libkrun


def _find_gvproxy() -> str | None:
    import glob
    import shutil

    found = shutil.which("gvproxy")
    if found:
        return found

    # Cellar installs: version-agnostic glob, pick the newest
    for pattern in (
        "/opt/homebrew/Cellar/podman/*/libexec/podman/gvproxy",
        "/usr/local/Cellar/podman/*/libexec/podman/gvproxy",
    ):
        matches = glob.glob(pattern)
        if matches:
            matches.sort(key=lambda p: Path(p).stat().st_mtime, reverse=True)
            return matches[0]

    # Static fallbacks (symlink layouts / custom installs)
    for candidate in (
        "/opt/homebrew/libexec/podman/gvproxy",
        "/usr/local/libexec/podman/gvproxy",
        "/opt/homebrew/opt/podman/libexec/podman/gvproxy",
        "/usr/local/opt/podman/libexec/podman/gvproxy",
    ):
        if Path(candidate).exists():
            return candidate

    return None


def _start_gvproxy(sock_path: str, ssh_host_port: int) -> subprocess.Popen:
    binary = _find_gvproxy()
    if binary is None:
        print(
            "gvproxy not found. libkrun networking on macOS requires gvproxy.\n"
            "Install it with: brew install podman",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd = [
        binary,
        "-listen-vfkit",
        f"unixgram://{sock_path}",
        "-ssh-port",
        str(ssh_host_port),
        "-mtu",
        "1500",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for _ in range(100):
        if Path(sock_path).exists():
            return proc
        time.sleep(0.05)

    proc.kill()
    print(
        "gvproxy started but failed to create its socket; "
        "check 'brew reinstall podman' or run with verbose logging.",
        file=sys.stderr,
    )
    sys.exit(1)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python -m smolvm.runtime._libkrun_launcher <config.json>", file=sys.stderr)
        return 2

    config = json.loads(Path(argv[1]).read_text())
    ssh_host_port = int(config.get("ssh_host_port") or 2222)
    kernel_format = int(config.get("kernel_format", KERNEL_FORMAT_RAW))

    gvproxy_proc: subprocess.Popen | None = None
    sock_path = f"/tmp/krun-gvproxy-{uuid.uuid4().hex[:8]}.sock"

    try:
        with KrunContext() as ctx:
            ctx.set_vm_config(int(config["vcpus"]), int(config["memory_mib"]))

            ctx.set_kernel(
                Path(config["kernel_path"]),
                config.get("cmdline", ""),
                initramfs=Path(config["initrd_path"]) if config.get("initrd_path") else None,
                kernel_format=kernel_format,
            )

            if config.get("rootfs_path"):
                ctx.set_root_disk(Path(config["rootfs_path"]))

            for entry in config.get("extra_disks", []):
                ctx.add_disk(
                    entry["block_id"],
                    Path(entry["path"]),
                    bool(entry.get("read_only", False)),
                )

            for port_cfg in config.get("vsock_ports", []):
                ctx.add_vsock_port(int(port_cfg["port"]), Path(port_cfg["uds_path"]))

            if config.get("env"):
                ctx.set_env(config["env"])

            # macOS: wire up gvproxy for guest networking
            if sys.platform == "darwin":
                gvproxy_proc = _start_gvproxy(sock_path, ssh_host_port)
                lib = _libkrun()
                lib.krun_set_gvproxy_path.argtypes = [ctypes.c_uint32, ctypes.c_char_p]
                lib.krun_set_gvproxy_path.restype = ctypes.c_int32
                rc = lib.krun_set_gvproxy_path(ctx.ctx_id, sock_path.encode())
                if rc < 0:
                    print(
                        "Failed to configure guest networking"
                        f" (krun_set_gvproxy_path returned {rc});"
                        " reinstall libkrun with"
                        " 'brew tap libkrun/krun && brew install libkrun/krun/libkrun'"
                        " or check gvproxy is running.",
                        file=sys.stderr,
                    )
                    return 1

            rc = ctx.start_enter()
            return rc if rc >= 0 else 1

    finally:
        if gvproxy_proc is not None:
            gvproxy_proc.kill()
        import contextlib

        with contextlib.suppress(OSError):
            Path(sock_path).unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
