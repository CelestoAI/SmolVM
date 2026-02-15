#!/usr/bin/env python3

# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Manual end-to-end test for SmolVM environment variable injection.

This script validates:
1. Startup env injection from ``VMConfig.env_vars``.
2. Dynamic env updates via ``inject_env_vars``.
3. Env removal via ``remove_env_vars``.
4. SSH endpoint health for both localhost forwarding and direct guest IP.

It prints concrete diagnostics so forwarding regressions are visible even when
fallback to direct guest IP keeps the workflow functional.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from smolvm.build import SSH_BOOT_ARGS, ImageBuilder
from smolvm.env import ENV_FILE, inject_env_vars, read_env_vars, remove_env_vars
from smolvm.facade import VM
from smolvm.ssh import SSHClient
from smolvm.types import VMConfig, VMState
from smolvm.utils import ensure_ssh_key

logger = logging.getLogger("smolvm-env-test")

INITIAL_ENV = {
    "INITIAL_A": "aaaa",
    "INITIAL_B": "bbbb",
}
DYNAMIC_ENV = {
    "DYNAMIC_X": "xxxx",
    "DYNAMIC_Y": "yyyy",
}
REMOVE_ORDER = ["INITIAL_A", "DYNAMIC_X", "INITIAL_B", "DYNAMIC_Y"]


@dataclass(slots=True)
class EndpointProbe:
    """Result of probing one SSH endpoint."""

    label: str
    host: str
    port: int
    ok: bool
    error: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manual SmolVM env injection test")
    parser.add_argument("--vm-id", default="test-env-vm", help="VM identifier to use")
    parser.add_argument(
        "--boot-timeout",
        type=float,
        default=30.0,
        help="Timeout in seconds for VM boot/wait_for_ssh",
    )
    parser.add_argument(
        "--per-endpoint-timeout",
        type=float,
        default=15.0,
        help="Timeout in seconds per SSH endpoint probe",
    )
    parser.add_argument(
        "--image-name",
        default="alpine-ssh-key",
        help="Cached image name to use/build",
    )
    parser.add_argument(
        "--keep-vm",
        action="store_true",
        help="Keep VM after test instead of deleting it",
    )
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help="Print host networking diagnostics on failure",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args()


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(message)s")


def cleanup_vm(vm_id: str) -> None:
    """Best-effort cleanup of an existing VM by ID."""
    try:
        vm = VM.from_id(vm_id)
    except Exception:
        return

    try:
        if vm.info.status == VMState.RUNNING:
            vm.stop()
    except Exception as e:
        logger.warning("Failed to stop stale VM '%s': %s", vm_id, e)

    try:
        vm.delete()
        logger.info("Deleted stale VM '%s'.", vm_id)
    except Exception as e:
        logger.warning("Failed to delete stale VM '%s': %s", vm_id, e)


def build_ssh_image(public_key_path: Path, image_name: str) -> tuple[Path, Path]:
    logger.info("Building/ensuring SSH image '%s'...", image_name)
    builder = ImageBuilder()
    kernel, rootfs = builder.build_alpine_ssh_key(public_key_path, name=image_name)
    logger.info("Kernel: %s", kernel)
    logger.info("Rootfs: %s", rootfs)
    return kernel, rootfs


def ssh_candidates(vm: VM) -> list[tuple[str, int, str]]:
    """Return SSH endpoint candidates in preferred order."""
    vm.refresh()
    info = vm.info
    if info.network is None:
        raise RuntimeError("VM has no network configuration")

    candidates: list[tuple[str, int, str]] = []
    if isinstance(info.network.ssh_host_port, int):
        candidates.append(("127.0.0.1", info.network.ssh_host_port, "localhost-forward"))
    candidates.append((info.network.guest_ip, 22, "guest-ip"))
    return candidates


def probe_ssh_endpoints(
    candidates: list[tuple[str, int, str]],
    key_path: str,
    timeout: float,
) -> tuple[SSHClient | None, list[EndpointProbe]]:
    probes: list[EndpointProbe] = []
    selected: SSHClient | None = None

    for host, port, label in candidates:
        client = SSHClient(host=host, user="root", port=port, key_path=key_path)
        try:
            client.wait_for_ssh(timeout=timeout, interval=2.0)
            probes.append(EndpointProbe(label=label, host=host, port=port, ok=True))
            if selected is None:
                selected = client
        except Exception as e:
            probes.append(
                EndpointProbe(
                    label=label,
                    host=host,
                    port=port,
                    ok=False,
                    error=str(e),
                )
            )

    return selected, probes


def print_probe_report(probes: list[EndpointProbe]) -> None:
    logger.info("\nSSH endpoint probe results:")
    for probe in probes:
        status = "OK" if probe.ok else "FAIL"
        logger.info("  - %-18s %s:%d => %s", probe.label, probe.host, probe.port, status)
        if probe.error:
            logger.info("      %s", probe.error)

    localhost_ok = any(p.label == "localhost-forward" and p.ok for p in probes)
    guest_ok = any(p.label == "guest-ip" and p.ok for p in probes)
    if not localhost_ok and guest_ok:
        logger.warning(
            "Localhost SSH forwarding failed while guest-IP SSH works. "
            "This indicates a host-side forwarding regression."
        )


def assert_env_state(actual: dict[str, str], expected: dict[str, str], stage: str) -> None:
    if actual == expected:
        logger.info("%s: env state OK (%d vars)", stage, len(actual))
        return

    missing_or_changed = {
        key: value for key, value in expected.items() if actual.get(key) != value
    }
    unexpected = {key: value for key, value in actual.items() if key not in expected}
    raise AssertionError(
        f"{stage}: environment mismatch. "
        f"missing_or_changed={missing_or_changed}, unexpected={unexpected}"
    )


def print_env_file(vm: VM, stage: str) -> None:
    result = vm.run(f"cat {ENV_FILE} 2>/dev/null || true")
    content = result.stdout.strip()
    logger.info("\n%s (%s):\n%s\n", stage, ENV_FILE, content or "<empty>")


def run_host_diagnostic(cmd: list[str], title: str) -> None:
    logger.info("\n[%s] $ %s", title, " ".join(cmd))
    try:
        result = subprocess.run(cmd, check=False, text=True, capture_output=True)
    except Exception as e:
        logger.info("  (failed to execute: %s)", e)
        return

    if result.stdout.strip():
        logger.info(result.stdout.strip())
    if result.stderr.strip():
        logger.info(result.stderr.strip())


def print_network_diagnostics(vm: VM) -> None:
    logger.info("\n--- Host Networking Diagnostics ---")
    try:
        vm.refresh()
        info = vm.info
    except Exception as e:
        logger.info("Unable to refresh VM info: %s", e)
        return

    if info.network is None:
        logger.info("VM has no network configuration")
        return

    guest_ip = info.network.guest_ip
    tap_device = info.network.tap_device
    ssh_host_port = info.network.ssh_host_port

    run_host_diagnostic(["ip", "route", "get", guest_ip], "route-to-guest")
    run_host_diagnostic(["ip", "addr", "show", tap_device], "tap-address")
    if isinstance(ssh_host_port, int):
        run_host_diagnostic(
            ["iptables", "-t", "nat", "-S", "OUTPUT"],
            "nat-output-rules",
        )
        run_host_diagnostic(["iptables", "-S", "FORWARD"], "forward-rules")


def main() -> int:
    args = parse_args()
    configure_logging(args.verbose)

    logger.info("Preparing SSH key...")
    private_key, public_key = ensure_ssh_key()

    kernel, rootfs = build_ssh_image(public_key, args.image_name)

    cleanup_vm(args.vm_id)

    config = VMConfig(
        vm_id=args.vm_id,
        kernel_path=kernel,
        rootfs_path=rootfs,
        boot_args=SSH_BOOT_ARGS,
        env_vars=dict(INITIAL_ENV),
    )

    vm: VM | None = None
    try:
        vm = VM(config, ssh_key_path=str(private_key))
        logger.info("Starting VM '%s'...", args.vm_id)
        vm.start(boot_timeout=args.boot_timeout)

        candidates = ssh_candidates(vm)
        ssh, probes = probe_ssh_endpoints(
            candidates,
            key_path=str(private_key),
            timeout=args.per_endpoint_timeout,
        )
        print_probe_report(probes)

        if ssh is None:
            raise RuntimeError("SSH unreachable on all endpoints")

        print_env_file(vm, "After start()")
        state = read_env_vars(ssh)
        assert_env_state(state, INITIAL_ENV, "start env injection")

        logger.info("Injecting dynamic vars: %s", ", ".join(sorted(DYNAMIC_ENV)))
        inject_env_vars(ssh, DYNAMIC_ENV, merge=True)
        expected = dict(INITIAL_ENV)
        expected.update(DYNAMIC_ENV)
        state = read_env_vars(ssh)
        assert_env_state(state, expected, "dynamic injection")
        print_env_file(vm, "After dynamic injection")

        for key in REMOVE_ORDER:
            logger.info("Removing env var: %s", key)
            removed = remove_env_vars(ssh, [key])
            if key not in removed:
                raise AssertionError(f"expected '{key}' to be removed, got {removed}")
            expected.pop(key, None)
            state = read_env_vars(ssh)
            assert_env_state(state, expected, f"after removing {key}")
            print_env_file(vm, f"After removing {key}")

        logger.info("\nPASS: env injection workflow succeeded.")
        return 0

    except Exception as e:
        logger.error("\nFAIL: %s", e)
        if vm is not None and args.diagnostics:
            print_network_diagnostics(vm)
        return 1

    finally:
        if args.keep_vm:
            logger.info("Keeping VM '%s' (--keep-vm).", args.vm_id)
        else:
            logger.info("Cleaning up VM '%s'...", args.vm_id)
            cleanup_vm(args.vm_id)


if __name__ == "__main__":
    raise SystemExit(main())
