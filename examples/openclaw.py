#!/usr/bin/env python3
"""Run OpenClaw 2026.9.1 in a disposable SmolVM sandbox.

For everyday use, prefer ``smolvm openclaw start`` followed by
``smolvm openclaw open``. This lower-level example shows the same runtime,
safe configuration transfer, and localhost-only dashboard flow with the SDK.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from smolvm import SSH_BOOT_ARGS, ImageBuilder, SmolVM, VMConfig
from smolvm.presets.openclaw import OPENCLAW_NODE_VERSION, OPENCLAW_VERSION
from smolvm.utils import ensure_ssh_key

GUEST_DASHBOARD_PORT = 18789
HOST_DASHBOARD_PORT = 18789
OPENCLAW_PREFIX = "/opt/openclaw"
VM_MEMORY_MIB = 2048


def _run_or_exit(vm: SmolVM, command: str, timeout: int = 300) -> str:
    """Run one guest command and stop with a readable error if it fails."""
    print(f"\n$ {command}")
    result = vm.run(command, timeout=timeout)
    if result.output:
        print(result.output)
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)
    if not result.ok:
        print(f"Command failed (exit {result.exit_code}): {command}", file=sys.stderr)
        raise SystemExit(result.exit_code)
    return result.output


def _host_env_vars() -> dict[str, str]:
    """Collect only the credentials OpenClaw knows how to use."""
    names = (
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "OPENCLAW_GATEWAY_TOKEN",
        "OPENCLAW_GATEWAY_PASSWORD",
    )
    return {name: value for name in names if (value := os.getenv(name, "").strip())}


def _copy_portable_config(vm: SmolVM) -> None:
    """Copy config files, but leave OpenClaw's SQLite state on the host."""
    source_dir = Path.home() / ".openclaw"
    copies = (
        (source_dir / "openclaw.json", "/root/.openclaw/openclaw.json"),
        (source_dir / ".env", "/root/.openclaw/.env"),
    )
    available = [(source, target) for source, target in copies if source.is_file()]
    if not available:
        return

    _run_or_exit(vm, "mkdir -p /root/.openclaw", timeout=30)
    for source, target in available:
        vm.upload_file(source, target)
        _run_or_exit(vm, f"chmod 600 {target}", timeout=30)


def _install_supported_node(vm: SmolVM) -> None:
    """Install the Node.js line supported by this OpenClaw release."""
    node_major = OPENCLAW_NODE_VERSION[0]
    minimum = ", ".join(str(part) for part in OPENCLAW_NODE_VERSION)
    required_text = ".".join(str(part) for part in OPENCLAW_NODE_VERSION)
    _run_or_exit(
        vm,
        "set -e; "
        "apt-get update -qq; "
        "apt-get install -y -qq --no-install-recommends curl ca-certificates gnupg git; "
        f"curl -fsSL https://deb.nodesource.com/setup_{node_major}.x | bash -; "
        "apt-get install -y -qq --no-install-recommends nodejs",
        timeout=600,
    )
    _run_or_exit(
        vm,
        "node -e '"
        'const current=process.versions.node.split(".").map(Number);'
        f"const minimum=[{minimum}];"
        f"const ok=current[0]==={node_major}&&(current[1]>minimum[1]||"
        "(current[1]===minimum[1]&&current[2]>=minimum[2]));"
        f'if(!ok){{console.error("Node {required_text} or newer within {node_major}.x is '
        'required; found "+'
        "process.versions.node);process.exit(1)}'",
        timeout=60,
    )


def _install_openclaw(vm: SmolVM) -> None:
    """Install the exact OpenClaw release and permit its package setup script."""
    _run_or_exit(vm, f"rm -rf {OPENCLAW_PREFIX} && mkdir -p {OPENCLAW_PREFIX}", timeout=60)
    _run_or_exit(
        vm,
        "set -e; "
        "npm_version=$(npm --version); "
        "npm_major=${npm_version%%.*}; npm_rest=${npm_version#*.}; "
        "npm_minor=${npm_rest%%.*}; npm_lifecycle_arg=; "
        'if [ "$npm_major" -ge 12 ] || '
        '{ [ "$npm_major" -eq 11 ] && [ "$npm_minor" -ge 16 ]; }; then '
        "npm_lifecycle_arg=--allow-scripts=openclaw; fi; "
        f"npm --prefix {OPENCLAW_PREFIX} install -g $npm_lifecycle_arg "
        f"openclaw@{OPENCLAW_VERSION}; "
        f"ln -sf {OPENCLAW_PREFIX}/bin/openclaw /usr/local/bin/openclaw; "
        f"openclaw --version | grep -F {OPENCLAW_VERSION}; "
        "npm cache clean --force >/dev/null 2>&1",
        timeout=1200,
    )


def _start_gateway(vm: SmolVM) -> None:
    """Start the loopback-only OpenClaw gateway and wait until it responds."""
    _run_or_exit(
        vm,
        "gateway_ready() { "
        f"curl --fail --silent --show-error --max-time 2 http://127.0.0.1:{GUEST_DASHBOARD_PORT}/ "
        ">/dev/null 2>&1 || "
        "curl --fail --silent --show-error --insecure --max-time 2 "
        f"https://127.0.0.1:{GUEST_DASHBOARD_PORT}/ >/dev/null 2>&1; "
        "}; "
        "if gateway_ready; "
        "then exit 0; fi; "
        "nohup openclaw gateway run --allow-unconfigured --bind loopback "
        f"--port {GUEST_DASHBOARD_PORT} "
        "</dev/null >/tmp/smolvm-openclaw-gateway.log 2>&1 & "
        'gateway_pid=$!; echo "$gateway_pid" >/tmp/smolvm-openclaw-gateway.pid; '
        "for attempt in $(seq 1 60); do "
        "gateway_ready && exit 0; "
        "sleep 0.5; done; "
        'kill "$gateway_pid" >/dev/null 2>&1 || true; '
        'wait "$gateway_pid" >/dev/null 2>&1 || true; '
        "rm -f /tmp/smolvm-openclaw-gateway.pid; "
        "tail -n 80 /tmp/smolvm-openclaw-gateway.log >&2; exit 1",
        timeout=60,
    )


def _dashboard_url(vm: SmolVM, host_port: int) -> str:
    """Create a one-time dashboard link and point it at the host port."""
    raw = _run_or_exit(
        vm,
        f"OPENCLAW_GATEWAY_PORT={GUEST_DASHBOARD_PORT} openclaw dashboard --json --no-open",
        timeout=30,
    )
    try:
        data = json.loads(raw)
        url = data.get("browserUrl") or data.get("url")
    except (AttributeError, json.JSONDecodeError) as exc:
        raise RuntimeError("OpenClaw returned an unreadable dashboard link.") from exc
    if not isinstance(url, str):
        raise RuntimeError("OpenClaw did not return a dashboard link.")

    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port != GUEST_DASHBOARD_PORT
    ):
        raise RuntimeError("OpenClaw returned an unexpected dashboard address.")
    return urlunsplit(
        (parsed.scheme, f"127.0.0.1:{host_port}", parsed.path, parsed.query, parsed.fragment)
    )


def main() -> int:
    env_vars = _host_env_vars()
    private_key, public_key = ensure_ssh_key()
    kernel, rootfs = ImageBuilder().build_debian_ssh_key(
        ssh_public_key=public_key,
        name="debian-ssh-key-openclaw-4g",
        rootfs_size_mb=4096,
    )
    config = VMConfig(
        vcpu_count=1,
        memory=VM_MEMORY_MIB,
        kernel_path=kernel,
        rootfs_path=rootfs,
        boot_args=SSH_BOOT_ARGS,
    )

    with SmolVM(config, ssh_key_path=str(private_key)) as vm:
        print(f"Sandbox running: {vm.vm_id} ({vm.get_ip()})")
        _install_supported_node(vm)
        _install_openclaw(vm)
        _copy_portable_config(vm)
        if env_vars:
            vm.set_env_vars(env_vars)
        _start_gateway(vm)

        host_port = vm.expose_local(
            GUEST_DASHBOARD_PORT,
            HOST_DASHBOARD_PORT,
            guest_loopback=True,
        )
        print("\nOpen this one-time OpenClaw link within 10 minutes:")
        print(_dashboard_url(vm, host_port))
        try:
            input("\nPress Enter to stop and delete the sandbox...")
        except EOFError:
            print("\nNo interactive input is available; deleting the sandbox now.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
