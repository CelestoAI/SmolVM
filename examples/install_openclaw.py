#!/usr/bin/env python3
"""Install OpenClaw inside a SmolVM guest using a 4GB rootfs image."""

from __future__ import annotations

import sys

from smolvm import SSH_BOOT_ARGS, VM, ImageBuilder, VMConfig
from smolvm.utils import ensure_ssh_key

GUEST_DASHBOARD_PORT = 18789
HOST_DASHBOARD_PORT = 18789


def _run_or_exit(vm: VM, command: str, timeout: int = 300) -> None:
    """Run a guest command, print output, and exit on failure."""
    print(f"\n$ {command}")
    result = vm.run(command, timeout=timeout)
    if result.stdout:
        print(result.stdout.strip())
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)
    if result.exit_code != 0:
        raise SystemExit(result.exit_code)


def main() -> int:
    private_key, public_key = ensure_ssh_key()
    kernel, rootfs = ImageBuilder().build_alpine_ssh_key(
        ssh_public_key=public_key,
        name="alpine-ssh-key-openclaw-4g",
        rootfs_size_mb=4096,
    )

    config = VMConfig(
        vm_id="openclaw-install",
        vcpu_count=1,
        mem_size_mib=512,
        kernel_path=kernel,
        rootfs_path=rootfs,
        boot_args=SSH_BOOT_ARGS,
    )

    with VM(config, ssh_key_path=str(private_key)) as vm:
        print(f"VM running at {vm.get_ip()}")
        _run_or_exit(vm, "df -h /", timeout=60)

        _run_or_exit(vm, "apk add --no-cache bash curl git nodejs npm", timeout=300)
        _run_or_exit(vm, "npm install -g openclaw", timeout=600)
        _run_or_exit(vm, "openclaw --version || which openclaw", timeout=60)

        # Start gateway dashboard endpoint in the guest.
        _run_or_exit(
            vm,
            (
                f"nohup openclaw gateway --port {GUEST_DASHBOARD_PORT} "
                ">/tmp/openclaw-gateway.log 2>&1 &"
            ),
            timeout=30,
        )
        _run_or_exit(
            vm,
            (
                f"for i in $(seq 1 30); do "
                f"curl -fsS http://127.0.0.1:{GUEST_DASHBOARD_PORT}/ >/dev/null && exit 0; "
                "sleep 1; "
                "done; "
                "echo 'Gateway did not start in time' >&2; "
                "tail -n 50 /tmp/openclaw-gateway.log >&2; "
                "exit 1"
            ),
            timeout=60,
        )

        host_port = vm.expose_local(
            guest_port=GUEST_DASHBOARD_PORT,
            host_port=HOST_DASHBOARD_PORT,
        )
        print(f"\nDashboard ready: http://127.0.0.1:{host_port}/ (localhost only)")

        # Helpful in headless mode: prints dashboard URL if browser open is unavailable.
        _run_or_exit(vm, "openclaw dashboard || true", timeout=60)

    print("\nOpenClaw install flow completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
