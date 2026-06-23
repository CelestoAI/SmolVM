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

"""Measure Linux host networking setup stages for TAP-backed sandboxes."""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
try:
    from .reporting import finish_report, print_report, start_report
except ImportError:  # pragma: no cover - script execution path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from reporting import finish_report, print_report, start_report  # type: ignore[no-redef]

logger = logging.getLogger("smolvm.bench.networking")

MODES = ("native", "forced-off", "unprivileged-fallback")
NATIVE_DISABLE_ENV = "SMOLVM_DISABLE_NATIVE_NETWORKING"
CAP_NET_ADMIN = 12
_NETWORK_MODULE: Any | None = None


def _load_network_module() -> Any:
    global _NETWORK_MODULE
    if _NETWORK_MODULE is None:
        import smolvm.host.network as network_module

        _NETWORK_MODULE = network_module
    return _NETWORK_MODULE


@contextmanager
def _native_mode(mode: str) -> Iterator[None]:
    network_module = _load_network_module()
    old_env = os.environ.get(NATIVE_DISABLE_ENV)
    old_latch = network_module._native_unprivileged
    network_module._native_unprivileged = False
    try:
        if mode == "forced-off":
            os.environ[NATIVE_DISABLE_ENV] = "1"
        else:
            os.environ.pop(NATIVE_DISABLE_ENV, None)
        yield
    finally:
        if old_env is None:
            os.environ.pop(NATIVE_DISABLE_ENV, None)
        else:
            os.environ[NATIVE_DISABLE_ENV] = old_env
        network_module._native_unprivileged = old_latch


def _time_stage(record: dict[str, Any], name: str, fn) -> Any:
    started = time.perf_counter()
    try:
        return fn()
    finally:
        record[f"{name}_ms"] = round((time.perf_counter() - started) * 1000, 1)


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _run_json(cmd: list[str]) -> Any:
    return json.loads(_run(cmd).stdout or "[]")


def _tap_exists(tap_name: str) -> bool:
    return _run(["ip", "link", "show", "dev", tap_name], check=False).returncode == 0


def _assert_tap_configured(tap_name: str, host_ip: str, prefix_len: int) -> None:
    links = _run_json(["ip", "-j", "addr", "show", "dev", tap_name])
    if not links:
        raise RuntimeError(f"TAP {tap_name} does not exist")
    link = links[0]
    if "UP" not in link.get("flags", []):
        raise RuntimeError(f"TAP {tap_name} is not up")
    addr_info = link.get("addr_info", [])
    expected = {"local": host_ip, "prefixlen": prefix_len}
    if not any(
        item.get("family") == "inet"
        and item.get("local") == expected["local"]
        and item.get("prefixlen") == expected["prefixlen"]
        for item in addr_info
    ):
        raise RuntimeError(f"TAP {tap_name} does not have {host_ip}/{prefix_len}")


def _assert_route(guest_ip: str, tap_name: str) -> None:
    routes = _run_json(["ip", "-j", "route", "show", f"{guest_ip}/32"])
    if not any(route.get("dev") == tap_name for route in routes):
        raise RuntimeError(f"Route {guest_ip}/32 does not use {tap_name}")


def _assert_sysctl(key_path: str, expected: str) -> None:
    value = Path("/proc/sys", key_path).read_text().strip()
    if value != expected:
        raise RuntimeError(f"sysctl {key_path} is {value}, expected {expected}")


def _sudo_fallback_available() -> bool:
    from smolvm.exceptions import SmolVMError
    from smolvm.utils import run_command

    checks = [
        ["ip", "link", "show"],
        ["sysctl", "net.ipv4.ip_forward"],
        ["nft", "list", "tables"],
    ]
    for cmd in checks:
        try:
            run_command(cmd, use_sudo=True)
        except SmolVMError:
            return False
    return True


def _has_direct_tap_privileges() -> bool:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("CapEff:"):
                effective_caps = int(line.split(":", 1)[1].strip(), 16)
                return bool(effective_caps & (1 << CAP_NET_ADMIN))
    except (OSError, ValueError):
        logger.debug("Could not read effective capabilities from /proc/self/status", exc_info=True)
    return os.geteuid() == 0


def _should_skip_mode(mode: str) -> str | None:
    if mode == "forced-off":
        return None
    network_module = _load_network_module()
    if mode == "native":
        if not network_module.HAS_NETLINK:
            return "native networking is unavailable"
        if network_module._native_unprivileged:
            return (
                "native networking hit EPERM and fell back; "
                "rerun this benchmark with sudo to measure the native speedup"
            )
        if _has_direct_tap_privileges():
            return None
        if not _sudo_fallback_available():
            return (
                "native mode needs root or CAP_NET_ADMIN; rerun this benchmark "
                "with sudo for native speed, or run `smolvm setup` to enable fallback"
            )
        return (
            "native mode needs root or CAP_NET_ADMIN; rerun this benchmark "
            "with sudo to measure the native speedup"
        )
    if mode != "unprivileged-fallback":
        return None
    if not network_module.HAS_NETLINK:
        return "native networking is unavailable, so EPERM fallback cannot be tested"
    if _has_direct_tap_privileges():
        return "unprivileged fallback needs a process without direct TAP privileges"
    if os.geteuid() == 0:
        return "unprivileged fallback needs a non-root process"
    if not _sudo_fallback_available():
        return "sudo fallback is unavailable; run `smolvm setup` before measuring fallback"
    return None


def _benchmark_network_stages(
    mode: str, *, include_full_start: bool, boot_timeout: float
) -> dict[str, Any]:
    suffix = f"{os.getpid() % 10000}{random.randint(10, 99)}"
    tap_name = f"svmb{suffix}"[:15]
    vm_id = f"bench-{suffix}"
    host_ip = "172.31.255.1"
    guest_ip = "172.31.255.2"
    host_port = 20000 + random.randint(0, 20000)
    record: dict[str, Any] = {
        "mode": mode,
        "tap_name": tap_name,
        "guest_ip": guest_ip,
        "host_port": host_port,
    }
    nm = _load_network_module().NetworkManager(host_ip=host_ip)

    with _native_mode(mode):
        skip_reason = _should_skip_mode(mode)
        if skip_reason is not None:
            record["skipped"] = skip_reason
            return record

        try:
            _time_stage(record, "default_interface", lambda: nm._detect_outbound_interface())
            _time_stage(
                record, "create_tap", lambda: nm.create_tap(tap_name, os.environ.get("USER"))
            )
            _time_stage(record, "configure_tap", lambda: nm.configure_tap(tap_name, host_ip, "32"))
            _assert_tap_configured(tap_name, host_ip, 32)
            _assert_sysctl(f"net/ipv4/conf/{tap_name}/route_localnet", "1")

            _time_stage(record, "add_route", lambda: nm.add_route(guest_ip, tap_name))
            _assert_route(guest_ip, tap_name)

            _time_stage(record, "enable_ip_forwarding", nm.enable_ip_forwarding)
            _assert_sysctl("net/ipv4/ip_forward", "1")

            _time_stage(record, "setup_nat_firewall", lambda: nm.setup_nat(tap_name))
            _time_stage(
                record,
                "setup_ssh_port_forward",
                lambda: nm.setup_ssh_port_forward(vm_id, guest_ip, host_port),
            )
            _time_stage(
                record,
                "cleanup_ssh_port_forward",
                lambda: nm.cleanup_ssh_port_forward(vm_id, guest_ip, host_port),
            )
        finally:
            with suppress(Exception):
                nm.cleanup_ssh_port_forward(vm_id, guest_ip, host_port)
            with suppress(Exception):
                nm.cleanup_nat_rules(tap_name)
            _time_stage(record, "delete_tap", lambda: nm.cleanup_tap(tap_name))

        if _tap_exists(tap_name):
            raise RuntimeError(f"TAP {tap_name} still exists after cleanup")

        skip_reason = _should_skip_mode(mode)
        if skip_reason is not None:
            record["skipped"] = skip_reason
            return record

        if include_full_start:
            full_start_ms = _benchmark_full_start(boot_timeout)
            skip_reason = _should_skip_mode(mode)
            if skip_reason is None:
                record["full_start_ms"] = full_start_ms
            else:
                record["full_start_skipped"] = skip_reason

    return record


def _benchmark_full_start(boot_timeout: float) -> float:
    from smolvm.facade import SmolVM

    vm = SmolVM(backend="firecracker", os="alpine", comm_channel="ssh")
    started = time.perf_counter()
    try:
        vm.start(boot_timeout=boot_timeout)
        vm.wait_for_ssh(timeout=boot_timeout)
        return round((time.perf_counter() - started) * 1000, 1)
    finally:
        with suppress(Exception):
            vm.stop(timeout=15.0)
        with suppress(Exception):
            vm.delete()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--modes",
        default="native,forced-off,unprivileged-fallback",
        help="Comma-separated modes to run: native, forced-off, unprivileged-fallback.",
    )
    parser.add_argument(
        "--include-full-start",
        action="store_true",
        help="Also boot one Firecracker TAP-backed sandbox per mode.",
    )
    parser.add_argument("--boot-timeout", type=float, default=180.0)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON only.")
    parser.add_argument("--output", type=Path, help="Optional JSON output path.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if sys.platform != "linux":
        raise SystemExit("This benchmark only runs on Linux.")

    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]
    unknown = sorted(set(modes) - set(MODES))
    if unknown:
        raise SystemExit(f"Unknown mode(s): {', '.join(unknown)}")

    config = {
        "modes": modes,
        "include_full_start": args.include_full_start,
        "boot_timeout": args.boot_timeout,
    }
    report, started = start_report("networking", config=config, dry_run=False)
    report["records"] = [
        _benchmark_network_stages(
            mode,
            include_full_start=args.include_full_start,
            boot_timeout=args.boot_timeout,
        )
        for mode in modes
    ]
    finish_report(report, started)
    print_report(
        report,
        json_output=args.json,
        output=args.output,
        human_lines=_human_lines(report),
    )
    return 0


def _human_lines(report: dict[str, Any]) -> list[str]:
    lines = ["Networking stages:"]
    for record in report["records"]:
        if "skipped" in record:
            lines.append(f"  {record['mode']}: skipped - {record['skipped']}")
            continue
        lines.append(f"  {record['mode']}:")
        for key, value in record.items():
            if key.endswith("_ms"):
                lines.append(f"    {key}: {value}")
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
