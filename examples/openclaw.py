#!/usr/bin/env python3
"""Install OpenClaw inside a Debian-based SmolVM guest (4GB rootfs)."""

from __future__ import annotations

import sys

from smolvm import SSH_BOOT_ARGS, VM, ImageBuilder, VMConfig
from smolvm.utils import ensure_ssh_key

GUEST_DASHBOARD_PORT = 18789
HOST_DASHBOARD_PORT = 18789
GATEWAY_TOKEN = "smolvm-local-token"
OPENCLAW_PREFIX = "/opt/openclaw"
VM_MEMORY_MIB = 2048


def _run_or_exit(vm: VM, command: str, timeout: int = 300) -> None:
    """Run a guest command, print output, and exit on failure."""
    print(f"\n$ {command}")
    result = vm.run(command, timeout=timeout)
    if result.stdout:
        print(result.stdout.strip())
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)
    if result.exit_code != 0:
        print(
            f"Command failed (exit {result.exit_code}): {command}",
            file=sys.stderr,
        )
        raise SystemExit(result.exit_code)


def _ensure_node_runtime(vm: VM) -> None:
    """Install Node.js/NPM and guarantee Node >= 22.12.0 for OpenClaw."""
    print("\n== Installing runtime dependencies ==")
    _run_or_exit(
        vm,
        (
            "apt-get update && "
            "apt-get install -y --no-install-recommends "
            "ca-certificates curl gnupg git bash && "
            "rm -rf /var/lib/apt/lists/*"
        ),
        timeout=300,
    )

    # OpenClaw currently requires Node >= 22.12.0.
    _run_or_exit(vm, "mkdir -p /etc/apt/keyrings", timeout=60)
    _run_or_exit(
        vm,
        (
            "curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key "
            "| gpg --dearmor --batch --yes -o /etc/apt/keyrings/nodesource.gpg"
        ),
        timeout=120,
    )
    _run_or_exit(
        vm,
        (
            "echo 'deb [signed-by=/etc/apt/keyrings/nodesource.gpg] "
            "https://deb.nodesource.com/node_22.x nodistro main' "
            "> /etc/apt/sources.list.d/nodesource.list"
        ),
        timeout=60,
    )
    _run_or_exit(
        vm,
        (
            "apt-get update && "
            "apt-get install -y --no-install-recommends nodejs && "
            "rm -rf /var/lib/apt/lists/*"
        ),
        timeout=300,
    )
    _run_or_exit(vm, "node -v && npm -v", timeout=60)
    _run_or_exit(
        vm,
        (
            "node -e \"const [maj,min]=process.versions.node.split('.').map(Number); "
            "if(maj<22||(maj===22&&min<12)){"
            "console.error('Node >=22.12.0 required, found '+process.versions.node);"
            "process.exit(1)"
            "}\""
        ),
        timeout=60,
    )


def _install_openclaw(vm: VM) -> None:
    """Install OpenClaw in an isolated npm prefix to avoid global path conflicts."""
    print("\n== Installing OpenClaw ==")
    _run_or_exit(vm, f"rm -rf {OPENCLAW_PREFIX}", timeout=60)
    _run_or_exit(vm, f"mkdir -p {OPENCLAW_PREFIX}", timeout=60)
    _run_or_exit(vm, "npm cache clean --force || true", timeout=120)
    _run_or_exit(
        vm,
        f"npm --prefix {OPENCLAW_PREFIX} install -g openclaw",
        timeout=1200,
    )
    _run_or_exit(
        vm,
        f"ln -sf {OPENCLAW_PREFIX}/bin/openclaw /usr/local/bin/openclaw",
        timeout=60,
    )
    print("\n== Verifying OpenClaw install ==")
    _run_or_exit(
        vm,
        "command -v openclaw >/dev/null || { echo 'openclaw not found in PATH' >&2; exit 1; }",
        timeout=60,
    )
    _run_or_exit(vm, "openclaw --help >/dev/null 2>&1 || true", timeout=60)


def main() -> int:
    private_key, public_key = ensure_ssh_key()
    kernel, rootfs = ImageBuilder().build_debian_ssh_key(
        ssh_public_key=public_key,
        name="debian-ssh-key-openclaw-4g",
        rootfs_size_mb=4096,
    )

    config = VMConfig(
        vm_id="openclaw-install",
        vcpu_count=1,
        # OpenClaw npm install is memory-heavy; 512 MiB can drop SSH mid-command.
        mem_size_mib=VM_MEMORY_MIB,
        kernel_path=kernel,
        rootfs_path=rootfs,
        boot_args=SSH_BOOT_ARGS,
    )

    with VM(config, ssh_key_path=str(private_key)) as vm:
        guest_ip = vm.get_ip()
        print(f"VM running at {guest_ip}")
        _run_or_exit(vm, "df -h /", timeout=60)

        _ensure_node_runtime(vm)
        _install_openclaw(vm)

        # Start gateway dashboard endpoint in the guest.
        _run_or_exit(
            vm,
            (
                f"nohup openclaw gateway --allow-unconfigured --token {GATEWAY_TOKEN} "
                f"--port {GUEST_DASHBOARD_PORT} "
                ">/tmp/openclaw-gateway.log 2>&1 &"
            ),
            timeout=30,
        )
        _run_or_exit(
            vm,
            (
                f"for i in $(seq 1 30); do "
                f"curl -sS -o /dev/null http://127.0.0.1:{GUEST_DASHBOARD_PORT}/ && exit 0; "
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
        if host_port != HOST_DASHBOARD_PORT:
            print(
                f"Preferred localhost port {HOST_DASHBOARD_PORT} was unavailable; "
                f"using {host_port} instead."
            )
        print(f"Gateway token: {GATEWAY_TOKEN}")

        # Helpful in headless mode: prints dashboard URL if browser open is unavailable.
        _run_or_exit(vm, "openclaw dashboard || true", timeout=60)
        try:
            input("\nPress Enter to stop and clean up the VM...")
        except EOFError:
            print("\nNo interactive input available; cleaning up now.")

    print("\nOpenClaw install flow completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
