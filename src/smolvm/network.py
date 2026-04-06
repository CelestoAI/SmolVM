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

"""Networking utilities for Linux Firecracker VMs.

This module manages TAP devices (iproute2) and firewall/NAT rules (nftables).
All public methods are idempotent and safe to call repeatedly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import time
from pathlib import Path
from urllib.parse import urlparse

from smolvm.exceptions import NetworkError, SmolVMError
from smolvm.utils import async_run_command, run_command

logger = logging.getLogger(__name__)

# Default network configuration
DEFAULT_HOST_IP = "172.16.0.1"
DEFAULT_NETMASK = "16"

# SmolVM-managed nftables objects
_NFT_NAT_FAMILY = "ip"
_NFT_NAT_TABLE = "smolvm_nat"
_NFT_FILTER_FAMILY = "inet"
_NFT_FILTER_TABLE = "smolvm_filter"

_NFT_CHAIN_RE = re.compile(r"^chain\s+(?P<chain>[^\s{]+)\s*\{")
_NFT_RULE_COMMENT_RE = re.compile(r'comment "(?P<comment>[^"]+)"')
_NFT_RULE_HANDLE_RE = re.compile(r'comment "(?P<comment>[^"]+)".*# handle (?P<handle>\d+)')


class NetworkManager:
    """Manage host networking resources for VM connectivity."""

    def __init__(self, host_ip: str = DEFAULT_HOST_IP) -> None:
        if not host_ip:
            raise ValueError("host_ip cannot be empty")

        self.host_ip = host_ip
        self._outbound_interface: str | None = None
        self._nft_base_ready = False
        self._ip_forwarding_enabled = False

    @property
    def outbound_interface(self) -> str:
        """Return the host's default outbound interface."""
        if self._outbound_interface is None:
            self._outbound_interface = self._detect_outbound_interface()
        return self._outbound_interface

    def _detect_outbound_interface(self) -> str:
        """Detect outbound interface from default route."""
        try:
            result = run_command(["ip", "route", "show", "default"], use_sudo=False)
            parts = result.stdout.strip().split()
            if "dev" in parts:
                idx = parts.index("dev")
                if idx + 1 < len(parts):
                    iface = parts[idx + 1]
                    logger.info("Detected outbound interface: %s", iface)
                    return iface
        except Exception as e:
            logger.error("Failed to detect outbound interface: %s", e)

        raise NetworkError("Could not detect default outbound network interface")

    async def _async_detect_outbound_interface(self) -> str:
        """Detect outbound interface from default route (async)."""
        try:
            result = await async_run_command(["ip", "route", "show", "default"], use_sudo=False)
            parts = result.stdout.strip().split()
            if "dev" in parts:
                idx = parts.index("dev")
                if idx + 1 < len(parts):
                    iface = parts[idx + 1]
                    logger.info("Detected outbound interface: %s", iface)
                    return iface
        except Exception as e:
            logger.error("Failed to detect outbound interface: %s", e)

        raise NetworkError("Could not detect default outbound network interface")

    # ------------------------------------------------------------------
    # TAP / routing
    # ------------------------------------------------------------------

    def create_tap(self, tap_name: str, user: str | None = None) -> None:
        """Create TAP device if missing."""
        if not tap_name:
            raise ValueError("tap_name cannot be empty")

        if user is None:
            user = os.environ.get("USER", "root")

        logger.info("Creating TAP device: %s (user: %s)", tap_name, user)

        max_busy_retries = 3
        for attempt in range(max_busy_retries + 1):
            try:
                run_command(["ip", "tuntap", "add", tap_name, "mode", "tap", "user", user])
                return
            except SmolVMError as e:
                err = str(e)
                if "File exists" in err or "EEXIST" in err:
                    logger.debug("TAP device %s already exists", tap_name)
                    return

                is_busy = "Device or resource busy" in err or "EBUSY" in err
                if is_busy and attempt < max_busy_retries:
                    # Kernel/device cleanup can be briefly asynchronous. Retry.
                    delay = 0.1 * (attempt + 1)
                    logger.warning(
                        "TAP %s busy during creation (attempt %d/%d), retrying in %.2fs",
                        tap_name,
                        attempt + 1,
                        max_busy_retries + 1,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                raise

    async def async_create_tap(self, tap_name: str, user: str | None = None) -> None:
        """Create TAP device if missing (async)."""
        if not tap_name:
            raise ValueError("tap_name cannot be empty")

        if user is None:
            user = os.environ.get("USER", "root")

        logger.info("Creating TAP device: %s (user: %s)", tap_name, user)

        max_busy_retries = 3
        for attempt in range(max_busy_retries + 1):
            try:
                await async_run_command(
                    ["ip", "tuntap", "add", tap_name, "mode", "tap", "user", user]
                )
                return
            except SmolVMError as e:
                err = str(e)
                if "File exists" in err or "EEXIST" in err:
                    logger.debug("TAP device %s already exists", tap_name)
                    return

                is_busy = "Device or resource busy" in err or "EBUSY" in err
                if is_busy and attempt < max_busy_retries:
                    delay = 0.1 * (attempt + 1)
                    logger.warning(
                        "TAP %s busy during creation (attempt %d/%d), retrying in %.2fs",
                        tap_name,
                        attempt + 1,
                        max_busy_retries + 1,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

    def configure_tap(
        self,
        tap_name: str,
        host_ip: str | None = None,
        netmask: str = DEFAULT_NETMASK,
    ) -> None:
        """Assign host IP and bring TAP link up."""
        if not tap_name:
            raise ValueError("tap_name cannot be empty")

        if host_ip is None:
            host_ip = self.host_ip

        logger.info("Configuring TAP %s with IP %s/%s", tap_name, host_ip, netmask)

        batch = [
            f"addr flush dev {tap_name}",
            f"addr add {host_ip}/{netmask} dev {tap_name}",
            f"link set {tap_name} up",
        ]

        try:
            self._run_ip_batch(batch)
        except SmolVMError as e:
            # Can happen in rare races; safe to ignore for idempotency.
            if "RTNETLINK answers: File exists" not in str(e):
                raise

        # Allow localhost DNAT to guest addresses.
        self._write_sysctl(f"net/ipv4/conf/{tap_name}/route_localnet", "1")

    async def async_configure_tap(
        self,
        tap_name: str,
        host_ip: str | None = None,
        netmask: str = DEFAULT_NETMASK,
    ) -> None:
        """Assign host IP and bring TAP link up (async)."""
        if not tap_name:
            raise ValueError("tap_name cannot be empty")

        if host_ip is None:
            host_ip = self.host_ip

        logger.info("Configuring TAP %s with IP %s/%s", tap_name, host_ip, netmask)

        batch = [
            f"addr flush dev {tap_name}",
            f"addr add {host_ip}/{netmask} dev {tap_name}",
            f"link set {tap_name} up",
        ]

        try:
            await self._async_run_ip_batch(batch)
        except SmolVMError as e:
            if "RTNETLINK answers: File exists" not in str(e):
                raise

        # Allow localhost DNAT to guest addresses.
        await self._async_write_sysctl(f"net/ipv4/conf/{tap_name}/route_localnet", "1")

    def add_route(self, ip_address: str, device: str) -> None:
        """Add host route for one guest IP through a TAP device."""
        if not ip_address:
            raise ValueError("ip_address cannot be empty")
        if not device:
            raise ValueError("device cannot be empty")

        logger.info("Adding route: %s via %s", ip_address, device)
        try:
            run_command(["ip", "route", "add", f"{ip_address}/32", "dev", device])
        except SmolVMError as e:
            if "File exists" not in str(e):
                raise

    async def async_add_route(self, ip_address: str, device: str) -> None:
        """Add host route for one guest IP through a TAP device (async)."""
        if not ip_address:
            raise ValueError("ip_address cannot be empty")
        if not device:
            raise ValueError("device cannot be empty")

        logger.info("Adding route: %s via %s", ip_address, device)
        try:
            await async_run_command(["ip", "route", "add", f"{ip_address}/32", "dev", device])
        except SmolVMError as e:
            if "File exists" not in str(e):
                raise

    def cleanup_tap(self, tap_name: str) -> None:
        """Delete TAP device (best effort)."""
        if not tap_name:
            raise ValueError("tap_name cannot be empty")

        logger.info("Cleaning up TAP device: %s", tap_name)
        try:
            run_command(["ip", "link", "delete", tap_name])
        except SmolVMError as e:
            if "Cannot find device" not in str(e):
                logger.warning("Failed to delete TAP %s: %s", tap_name, e)

    async def async_cleanup_tap(self, tap_name: str) -> None:
        """Delete TAP device (best effort, async)."""
        if not tap_name:
            raise ValueError("tap_name cannot be empty")

        logger.info("Cleaning up TAP device: %s", tap_name)
        try:
            await async_run_command(["ip", "link", "delete", tap_name])
        except SmolVMError as e:
            if "Cannot find device" not in str(e):
                logger.warning("Failed to delete TAP %s: %s", tap_name, e)

    # ------------------------------------------------------------------
    # sysctl helpers
    # ------------------------------------------------------------------

    def enable_ip_forwarding(self) -> None:
        """Enable IPv4 forwarding once per manager instance."""
        if self._ip_forwarding_enabled:
            return

        if self._write_sysctl("net/ipv4/ip_forward", "1"):
            self._ip_forwarding_enabled = True

    async def async_enable_ip_forwarding(self) -> None:
        """Enable IPv4 forwarding once per manager instance (async)."""
        if self._ip_forwarding_enabled:
            return

        if await self._async_write_sysctl("net/ipv4/ip_forward", "1"):
            self._ip_forwarding_enabled = True

    def _write_sysctl(self, key_path: str, value: str) -> bool:
        """Write /proc/sys key, with sudo sysctl fallback."""
        path = Path(f"/proc/sys/{key_path}")

        try:
            path.write_text(value)
            return True
        except (PermissionError, FileNotFoundError):
            pass

        key = key_path.replace("/", ".")
        try:
            run_command(["sysctl", "-w", f"{key}={value}"], use_sudo=True)
            return True
        except Exception as e:
            logger.warning("Failed to set sysctl %s: %s", key, e)
            return False

    async def _async_write_sysctl(self, key_path: str, value: str) -> bool:
        """Write /proc/sys key, with sudo sysctl fallback (async)."""
        path = Path(f"/proc/sys/{key_path}")

        try:
            path.write_text(value)
            return True
        except (PermissionError, FileNotFoundError):
            pass

        key = key_path.replace("/", ".")
        try:
            await async_run_command(["sysctl", "-w", f"{key}={value}"], use_sudo=True)
            return True
        except Exception as e:
            logger.warning("Failed to set sysctl %s: %s", key, e)
            return False

    def _run_ip_batch(self, commands: list[str]) -> None:
        """Execute batched iproute2 commands."""
        if not commands:
            return

        run_command(["ip", "-batch", "-"], input="\n".join(commands), use_sudo=True)

    async def _async_run_ip_batch(self, commands: list[str]) -> None:
        """Execute batched iproute2 commands (async)."""
        if not commands:
            return

        await async_run_command(["ip", "-batch", "-"], input="\n".join(commands), use_sudo=True)

    # ------------------------------------------------------------------
    # nftables helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _quote(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def _run_nft_script(self, script: str) -> None:
        run_command(["nft", "-f", "-"], input=script, use_sudo=True)

    async def _async_run_nft_script(self, script: str) -> None:
        await async_run_command(["nft", "-f", "-"], input=script, use_sudo=True)

    def _nft_table_exists(self, family: str, table: str) -> bool:
        try:
            run_command(["nft", "list", "table", family, table], use_sudo=True)
            return True
        except SmolVMError:
            return False

    async def _async_nft_table_exists(self, family: str, table: str) -> bool:
        try:
            await async_run_command(["nft", "list", "table", family, table], use_sudo=True)
            return True
        except SmolVMError:
            return False

    def _nft_chain_exists(self, family: str, table: str, chain: str) -> bool:
        try:
            run_command(["nft", "list", "chain", family, table, chain], use_sudo=True)
            return True
        except SmolVMError:
            return False

    async def _async_nft_chain_exists(self, family: str, table: str, chain: str) -> bool:
        try:
            await async_run_command(["nft", "list", "chain", family, table, chain], use_sudo=True)
            return True
        except SmolVMError:
            return False

    def _ensure_nftables_base(self) -> None:
        """Create SmolVM nftables tables/chains if missing.

        This is executed once per manager instance and uses a single batched
        nft script for any missing objects.
        """
        if self._nft_base_ready:
            return

        script_lines: list[str] = []

        nat_exists = self._nft_table_exists(_NFT_NAT_FAMILY, _NFT_NAT_TABLE)
        if not nat_exists:
            script_lines.extend(
                [
                    f"add table {_NFT_NAT_FAMILY} {_NFT_NAT_TABLE}",
                    (
                        f"add chain {_NFT_NAT_FAMILY} {_NFT_NAT_TABLE} prerouting "
                        "{ type nat hook prerouting priority dstnat; policy accept; }"
                    ),
                    (
                        f"add chain {_NFT_NAT_FAMILY} {_NFT_NAT_TABLE} output "
                        "{ type nat hook output priority -100; policy accept; }"
                    ),
                    (
                        f"add chain {_NFT_NAT_FAMILY} {_NFT_NAT_TABLE} postrouting "
                        "{ type nat hook postrouting priority srcnat; policy accept; }"
                    ),
                ]
            )
        else:
            if not self._nft_chain_exists(_NFT_NAT_FAMILY, _NFT_NAT_TABLE, "prerouting"):
                script_lines.append(
                    f"add chain {_NFT_NAT_FAMILY} {_NFT_NAT_TABLE} prerouting "
                    "{ type nat hook prerouting priority dstnat; policy accept; }"
                )
            if not self._nft_chain_exists(_NFT_NAT_FAMILY, _NFT_NAT_TABLE, "output"):
                script_lines.append(
                    f"add chain {_NFT_NAT_FAMILY} {_NFT_NAT_TABLE} output "
                    "{ type nat hook output priority -100; policy accept; }"
                )
            if not self._nft_chain_exists(_NFT_NAT_FAMILY, _NFT_NAT_TABLE, "postrouting"):
                script_lines.append(
                    f"add chain {_NFT_NAT_FAMILY} {_NFT_NAT_TABLE} postrouting "
                    "{ type nat hook postrouting priority srcnat; policy accept; }"
                )

        filter_exists = self._nft_table_exists(_NFT_FILTER_FAMILY, _NFT_FILTER_TABLE)
        if not filter_exists:
            script_lines.extend(
                [
                    f"add table {_NFT_FILTER_FAMILY} {_NFT_FILTER_TABLE}",
                    (
                        f"add chain {_NFT_FILTER_FAMILY} {_NFT_FILTER_TABLE} forward "
                        "{ type filter hook forward priority filter; policy accept; }"
                    ),
                ]
            )
        elif not self._nft_chain_exists(_NFT_FILTER_FAMILY, _NFT_FILTER_TABLE, "forward"):
            script_lines.append(
                f"add chain {_NFT_FILTER_FAMILY} {_NFT_FILTER_TABLE} forward "
                "{ type filter hook forward priority filter; policy accept; }"
            )

        if script_lines:
            self._run_nft_script("\n".join(script_lines) + "\n")

        self._nft_base_ready = True

    async def _async_ensure_nftables_base(self) -> None:
        """Create SmolVM nftables tables/chains if missing (async).

        This is executed once per manager instance and uses a single batched
        nft script for any missing objects.
        """
        if self._nft_base_ready:
            return

        script_lines: list[str] = []

        nat_exists = await self._async_nft_table_exists(_NFT_NAT_FAMILY, _NFT_NAT_TABLE)
        if not nat_exists:
            script_lines.extend(
                [
                    f"add table {_NFT_NAT_FAMILY} {_NFT_NAT_TABLE}",
                    (
                        f"add chain {_NFT_NAT_FAMILY} {_NFT_NAT_TABLE} prerouting "
                        "{ type nat hook prerouting priority dstnat; policy accept; }"
                    ),
                    (
                        f"add chain {_NFT_NAT_FAMILY} {_NFT_NAT_TABLE} output "
                        "{ type nat hook output priority -100; policy accept; }"
                    ),
                    (
                        f"add chain {_NFT_NAT_FAMILY} {_NFT_NAT_TABLE} postrouting "
                        "{ type nat hook postrouting priority srcnat; policy accept; }"
                    ),
                ]
            )
        else:
            nat_pre = await self._async_nft_chain_exists(
                _NFT_NAT_FAMILY, _NFT_NAT_TABLE, "prerouting"
            )
            if not nat_pre:
                script_lines.append(
                    f"add chain {_NFT_NAT_FAMILY} {_NFT_NAT_TABLE} prerouting "
                    "{ type nat hook prerouting priority dstnat; policy accept; }"
                )
            nat_out = await self._async_nft_chain_exists(
                _NFT_NAT_FAMILY, _NFT_NAT_TABLE, "output"
            )
            if not nat_out:
                script_lines.append(
                    f"add chain {_NFT_NAT_FAMILY} {_NFT_NAT_TABLE} output "
                    "{ type nat hook output priority -100; policy accept; }"
                )
            nat_post = await self._async_nft_chain_exists(
                _NFT_NAT_FAMILY, _NFT_NAT_TABLE, "postrouting"
            )
            if not nat_post:
                script_lines.append(
                    f"add chain {_NFT_NAT_FAMILY} {_NFT_NAT_TABLE} postrouting "
                    "{ type nat hook postrouting priority srcnat; policy accept; }"
                )

        filter_exists = await self._async_nft_table_exists(
            _NFT_FILTER_FAMILY, _NFT_FILTER_TABLE
        )
        if not filter_exists:
            script_lines.extend(
                [
                    f"add table {_NFT_FILTER_FAMILY} {_NFT_FILTER_TABLE}",
                    f"add chain {_NFT_FILTER_FAMILY} {_NFT_FILTER_TABLE} forward "
                    "{ type filter hook forward priority filter; policy accept; }",
                ]
            )
        elif not await self._async_nft_chain_exists(
            _NFT_FILTER_FAMILY, _NFT_FILTER_TABLE, "forward"
        ):
            script_lines.append(
                f"add chain {_NFT_FILTER_FAMILY} {_NFT_FILTER_TABLE} forward "
                "{ type filter hook forward priority filter; policy accept; }"
            )

        if script_lines:
            await self._async_run_nft_script("\n".join(script_lines) + "\n")

        self._nft_base_ready = True

    def _nft_list_table(self, family: str, table: str, *, handles: bool) -> str:
        cmd = ["nft"]
        if handles:
            cmd.append("-a")
        cmd.extend(["list", "table", family, table])

        try:
            result = run_command(cmd, use_sudo=True)
            return result.stdout
        except SmolVMError:
            return ""

    async def _async_nft_list_table(self, family: str, table: str, *, handles: bool) -> str:
        cmd = ["nft"]
        if handles:
            cmd.append("-a")
        cmd.extend(["list", "table", family, table])

        try:
            result = await async_run_command(cmd, use_sudo=True)
            return result.stdout
        except SmolVMError:
            return ""

    @staticmethod
    def _extract_table_comments(output: str) -> set[tuple[str, str]]:
        """Return existing (chain, comment) pairs for one nft table listing."""
        comments: set[tuple[str, str]] = set()
        current_chain: str | None = None

        for line in output.splitlines():
            stripped = line.strip()
            chain_match = _NFT_CHAIN_RE.match(stripped)
            if chain_match is not None:
                current_chain = chain_match.group("chain")
                continue

            if stripped == "}":
                current_chain = None
                continue

            if current_chain is None:
                continue

            comment_match = _NFT_RULE_COMMENT_RE.search(stripped)
            if comment_match is not None:
                comments.add((current_chain, comment_match.group("comment")))

        return comments

    @staticmethod
    def _extract_table_rule_handles(output: str) -> list[tuple[str, str, str]]:
        """Return (chain, comment, handle) tuples from an nft table listing."""
        handles: list[tuple[str, str, str]] = []
        current_chain: str | None = None

        for line in output.splitlines():
            stripped = line.strip()
            chain_match = _NFT_CHAIN_RE.match(stripped)
            if chain_match is not None:
                current_chain = chain_match.group("chain")
                continue

            if stripped == "}":
                current_chain = None
                continue

            if current_chain is None:
                continue

            handle_match = _NFT_RULE_HANDLE_RE.search(stripped)
            if handle_match is None:
                continue

            handles.append(
                (
                    current_chain,
                    handle_match.group("comment"),
                    handle_match.group("handle"),
                )
            )

        return handles

    def _add_nft_rules_if_missing(
        self,
        rules: list[tuple[str, str, str, str, str]],
    ) -> None:
        """Add rules in one batch, skipping existing (chain, comment) pairs."""
        table_comments_cache: dict[tuple[str, str], set[tuple[str, str]]] = {}
        script_lines: list[str] = []

        for family, table, chain, rule_expr, comment in rules:
            table_key = (family, table)
            if table_key not in table_comments_cache:
                table_output = self._nft_list_table(family, table, handles=False)
                table_comments_cache[table_key] = self._extract_table_comments(table_output)

            comment_key = (chain, comment)
            if comment_key in table_comments_cache[table_key]:
                continue

            script_lines.append(
                f"add rule {family} {table} {chain} {rule_expr} comment {self._quote(comment)}"
            )
            table_comments_cache[table_key].add(comment_key)

        if script_lines:
            self._run_nft_script("\n".join(script_lines) + "\n")

    async def _async_add_nft_rules_if_missing(
        self,
        rules: list[tuple[str, str, str, str, str]],
    ) -> None:
        """Add rules in one batch, skipping existing (chain, comment) pairs (async)."""
        table_comments_cache: dict[tuple[str, str], set[tuple[str, str]]] = {}
        script_lines: list[str] = []

        for family, table, chain, rule_expr, comment in rules:
            table_key = (family, table)
            if table_key not in table_comments_cache:
                table_output = await self._async_nft_list_table(family, table, handles=False)
                table_comments_cache[table_key] = self._extract_table_comments(table_output)

            comment_key = (chain, comment)
            if comment_key in table_comments_cache[table_key]:
                continue

            script_lines.append(
                f"add rule {family} {table} {chain} {rule_expr} comment {self._quote(comment)}"
            )
            table_comments_cache[table_key].add(comment_key)

        if script_lines:
            await self._async_run_nft_script("\n".join(script_lines) + "\n")

    def _delete_nft_rules(
        self,
        family: str,
        table: str,
        *,
        comment: str | None = None,
        comment_prefix: str | None = None,
    ) -> None:
        """Delete matching rules in one batched nft call."""
        if comment is None and comment_prefix is None:
            raise ValueError("comment or comment_prefix must be provided")

        table_output = self._nft_list_table(family, table, handles=True)
        if not table_output:
            return

        handles = self._extract_table_rule_handles(table_output)
        delete_lines: list[str] = []

        for chain, rule_comment, handle in handles:
            if comment is not None and rule_comment != comment:
                continue
            if comment_prefix is not None and not rule_comment.startswith(comment_prefix):
                continue
            delete_lines.append(f"delete rule {family} {table} {chain} handle {handle}")

        if delete_lines:
            self._run_nft_script("\n".join(delete_lines) + "\n")

    async def _async_delete_nft_rules(
        self,
        family: str,
        table: str,
        *,
        comment: str | None = None,
        comment_prefix: str | None = None,
    ) -> None:
        """Delete matching rules in one batched nft call (async)."""
        if comment is None and comment_prefix is None:
            raise ValueError("comment or comment_prefix must be provided")

        table_output = await self._async_nft_list_table(family, table, handles=True)
        if not table_output:
            return

        handles = self._extract_table_rule_handles(table_output)
        delete_lines: list[str] = []

        for chain, rule_comment, handle in handles:
            if comment is not None and rule_comment != comment:
                continue
            if comment_prefix is not None and not rule_comment.startswith(comment_prefix):
                continue
            delete_lines.append(f"delete rule {family} {table} {chain} handle {handle}")

        if delete_lines:
            await self._async_run_nft_script("\n".join(delete_lines) + "\n")

    def _find_nft_delete_rule_lines(
        self,
        family: str,
        table: str,
        *,
        comment: str | None = None,
        comment_prefix: str | None = None,
    ) -> list[str]:
        """Return nft 'delete rule' lines for rules matching comment filters."""
        if comment is None and comment_prefix is None:
            raise ValueError("comment or comment_prefix must be provided")

        table_output = self._nft_list_table(family, table, handles=True)
        if not table_output:
            return []

        handles = self._extract_table_rule_handles(table_output)
        delete_lines: list[str] = []
        for chain, rule_comment, handle in handles:
            if comment is not None and rule_comment != comment:
                continue
            if comment_prefix is not None and not rule_comment.startswith(comment_prefix):
                continue
            delete_lines.append(f"delete rule {family} {table} {chain} handle {handle}")

        return delete_lines

    async def _async_find_nft_delete_rule_lines(
        self,
        family: str,
        table: str,
        *,
        comment: str | None = None,
        comment_prefix: str | None = None,
    ) -> list[str]:
        """Return nft 'delete rule' lines for rules matching comment filters (async)."""
        if comment is None and comment_prefix is None:
            raise ValueError("comment or comment_prefix must be provided")

        table_output = await self._async_nft_list_table(family, table, handles=True)
        if not table_output:
            return []

        handles = self._extract_table_rule_handles(table_output)
        delete_lines: list[str] = []
        for chain, rule_comment, handle in handles:
            if comment is not None and rule_comment != comment:
                continue
            if comment_prefix is not None and not rule_comment.startswith(comment_prefix):
                continue
            delete_lines.append(f"delete rule {family} {table} {chain} handle {handle}")

        return delete_lines

    # ------------------------------------------------------------------
    # Public firewall/NAT API
    # ------------------------------------------------------------------

    def setup_nat(self, tap_name: str) -> None:
        """Configure outbound NAT and forwarding for a TAP device."""
        if not tap_name:
            raise ValueError("tap_name cannot be empty")

        logger.info("Setting up NAT for TAP: %s", tap_name)

        self.enable_ip_forwarding()
        self._ensure_nftables_base()

        iface = self.outbound_interface

        self._add_nft_rules_if_missing(
            [
                (
                    _NFT_NAT_FAMILY,
                    _NFT_NAT_TABLE,
                    "postrouting",
                    f"oifname {self._quote(iface)} counter masquerade",
                    f"smolvm:global:nat:masquerade:{iface}",
                ),
                (
                    _NFT_FILTER_FAMILY,
                    _NFT_FILTER_TABLE,
                    "forward",
                    "ct state related,established counter accept",
                    "smolvm:global:forward:established",
                ),
                (
                    _NFT_FILTER_FAMILY,
                    _NFT_FILTER_TABLE,
                    "forward",
                    (
                        f"iifname {self._quote(tap_name)} "
                        f"oifname {self._quote(iface)} counter accept"
                    ),
                    f"smolvm:nat:tap:{tap_name}:to:{iface}",
                ),
                (
                    _NFT_FILTER_FAMILY,
                    _NFT_FILTER_TABLE,
                    "forward",
                    (f"iifname {self._quote('tap*')} oifname {self._quote('tap*')} counter drop"),
                    "smolvm:global:forward:tap-isolation",
                ),
            ]
        )

    async def async_setup_nat(self, tap_name: str) -> None:
        """Configure outbound NAT and forwarding for a TAP device (async)."""
        if not tap_name:
            raise ValueError("tap_name cannot be empty")

        logger.info("Setting up NAT for TAP: %s", tap_name)

        await self.async_enable_ip_forwarding()
        await self._async_ensure_nftables_base()

        if self._outbound_interface is None:
            self._outbound_interface = await self._async_detect_outbound_interface()
        iface = self._outbound_interface

        await self._async_add_nft_rules_if_missing(
            [
                (
                    _NFT_NAT_FAMILY,
                    _NFT_NAT_TABLE,
                    "postrouting",
                    f"oifname {self._quote(iface)} counter masquerade",
                    f"smolvm:global:nat:masquerade:{iface}",
                ),
                (
                    _NFT_FILTER_FAMILY,
                    _NFT_FILTER_TABLE,
                    "forward",
                    "ct state related,established counter accept",
                    "smolvm:global:forward:established",
                ),
                (
                    _NFT_FILTER_FAMILY,
                    _NFT_FILTER_TABLE,
                    "forward",
                    (
                        f"iifname {self._quote(tap_name)} "
                        f"oifname {self._quote(iface)} counter accept"
                    ),
                    f"smolvm:nat:tap:{tap_name}:to:{iface}",
                ),
                (
                    _NFT_FILTER_FAMILY,
                    _NFT_FILTER_TABLE,
                    "forward",
                    (f"iifname {self._quote('tap*')} oifname {self._quote('tap*')} counter drop"),
                    "smolvm:global:forward:tap-isolation",
                ),
            ]
        )

    def setup_ssh_port_forward(
        self,
        vm_id: str,
        guest_ip: str,
        host_port: int,
        guest_port: int = 22,
    ) -> None:
        """Expose host TCP port to guest SSH port via nftables."""
        if not vm_id:
            raise ValueError("vm_id cannot be empty")
        if not guest_ip:
            raise ValueError("guest_ip cannot be empty")
        if host_port < 1 or host_port > 65535:
            raise ValueError("host_port must be 1-65535")
        if guest_port < 1 or guest_port > 65535:
            raise ValueError("guest_port must be 1-65535")

        self.enable_ip_forwarding()
        self._ensure_nftables_base()

        iface = self.outbound_interface
        target = f"{guest_ip}:{guest_port}"
        comment = f"smolvm:{vm_id}:ssh"

        self._add_nft_rules_if_missing(
            [
                (
                    _NFT_NAT_FAMILY,
                    _NFT_NAT_TABLE,
                    "prerouting",
                    (
                        f"iifname {self._quote(iface)} "
                        f"tcp dport {host_port} counter dnat to {target}"
                    ),
                    comment,
                ),
                (
                    _NFT_NAT_FAMILY,
                    _NFT_NAT_TABLE,
                    "output",
                    f"ip daddr 127.0.0.1/32 tcp dport {host_port} counter dnat to {target}",
                    comment,
                ),
                (
                    _NFT_NAT_FAMILY,
                    _NFT_NAT_TABLE,
                    "postrouting",
                    (
                        f"ip saddr 127.0.0.0/8 ip daddr {guest_ip}/32 "
                        f"tcp dport {guest_port} counter snat to {self.host_ip}"
                    ),
                    comment,
                ),
                (
                    _NFT_FILTER_FAMILY,
                    _NFT_FILTER_TABLE,
                    "forward",
                    (
                        f"ip daddr {guest_ip}/32 tcp dport {guest_port} "
                        "ct state new,related,established counter accept"
                    ),
                    comment,
                ),
            ]
        )

    async def async_setup_ssh_port_forward(
        self,
        vm_id: str,
        guest_ip: str,
        host_port: int,
        guest_port: int = 22,
    ) -> None:
        """Expose host TCP port to guest SSH port via nftables (async)."""
        if not vm_id:
            raise ValueError("vm_id cannot be empty")
        if not guest_ip:
            raise ValueError("guest_ip cannot be empty")
        if host_port < 1 or host_port > 65535:
            raise ValueError("host_port must be 1-65535")
        if guest_port < 1 or guest_port > 65535:
            raise ValueError("guest_port must be 1-65535")

        await self.async_enable_ip_forwarding()
        await self._async_ensure_nftables_base()

        if self._outbound_interface is None:
            self._outbound_interface = await self._async_detect_outbound_interface()
        iface = self._outbound_interface

        target = f"{guest_ip}:{guest_port}"
        comment = f"smolvm:{vm_id}:ssh"

        await self._async_add_nft_rules_if_missing(
            [
                (
                    _NFT_NAT_FAMILY,
                    _NFT_NAT_TABLE,
                    "prerouting",
                    (
                        f"iifname {self._quote(iface)} "
                        f"tcp dport {host_port} counter dnat to {target}"
                    ),
                    comment,
                ),
                (
                    _NFT_NAT_FAMILY,
                    _NFT_NAT_TABLE,
                    "output",
                    f"ip daddr 127.0.0.1/32 tcp dport {host_port} counter dnat to {target}",
                    comment,
                ),
                (
                    _NFT_NAT_FAMILY,
                    _NFT_NAT_TABLE,
                    "postrouting",
                    (
                        f"ip saddr 127.0.0.0/8 ip daddr {guest_ip}/32 "
                        f"tcp dport {guest_port} counter snat to {self.host_ip}"
                    ),
                    comment,
                ),
                (
                    _NFT_FILTER_FAMILY,
                    _NFT_FILTER_TABLE,
                    "forward",
                    (
                        f"ip daddr {guest_ip}/32 tcp dport {guest_port} "
                        "ct state new,related,established counter accept"
                    ),
                    comment,
                ),
            ]
        )

    def cleanup_ssh_port_forward(
        self,
        vm_id: str,
        guest_ip: str,
        host_port: int,
        guest_port: int = 22,
    ) -> None:
        """Remove SSH forwarding rules for one VM."""
        if not vm_id:
            raise ValueError("vm_id cannot be empty")
        if not guest_ip:
            raise ValueError("guest_ip cannot be empty")
        if host_port < 1 or host_port > 65535:
            raise ValueError("host_port must be 1-65535")
        if guest_port < 1 or guest_port > 65535:
            raise ValueError("guest_port must be 1-65535")

        comment = f"smolvm:{vm_id}:ssh"

        self._delete_nft_rules(_NFT_NAT_FAMILY, _NFT_NAT_TABLE, comment=comment)
        self._delete_nft_rules(_NFT_FILTER_FAMILY, _NFT_FILTER_TABLE, comment=comment)

    async def async_cleanup_ssh_port_forward(
        self,
        vm_id: str,
        guest_ip: str,
        host_port: int,
        guest_port: int = 22,
    ) -> None:
        """Remove SSH forwarding rules for one VM (async)."""
        if not vm_id:
            raise ValueError("vm_id cannot be empty")
        if not guest_ip:
            raise ValueError("guest_ip cannot be empty")
        if host_port < 1 or host_port > 65535:
            raise ValueError("host_port must be 1-65535")
        if guest_port < 1 or guest_port > 65535:
            raise ValueError("guest_port must be 1-65535")

        comment = f"smolvm:{vm_id}:ssh"

        await self._async_delete_nft_rules(_NFT_NAT_FAMILY, _NFT_NAT_TABLE, comment=comment)
        await self._async_delete_nft_rules(_NFT_FILTER_FAMILY, _NFT_FILTER_TABLE, comment=comment)

    def setup_local_port_forward(
        self,
        vm_id: str,
        guest_ip: str,
        host_port: int,
        guest_port: int,
    ) -> None:
        """Expose localhost:host_port to guest_ip:guest_port."""
        if not vm_id:
            raise ValueError("vm_id cannot be empty")
        if not guest_ip:
            raise ValueError("guest_ip cannot be empty")
        if host_port < 1 or host_port > 65535:
            raise ValueError("host_port must be 1-65535")
        if guest_port < 1 or guest_port > 65535:
            raise ValueError("guest_port must be 1-65535")

        self.enable_ip_forwarding()
        self._ensure_nftables_base()

        comment = f"smolvm:{vm_id}:local:{host_port}:{guest_port}"
        target = f"{guest_ip}:{guest_port}"

        self._add_nft_rules_if_missing(
            [
                (
                    _NFT_NAT_FAMILY,
                    _NFT_NAT_TABLE,
                    "output",
                    f"ip daddr 127.0.0.1/32 tcp dport {host_port} counter dnat to {target}",
                    comment,
                ),
                (
                    _NFT_NAT_FAMILY,
                    _NFT_NAT_TABLE,
                    "postrouting",
                    (
                        f"ip saddr 127.0.0.0/8 ip daddr {guest_ip}/32 "
                        f"tcp dport {guest_port} counter snat to {self.host_ip}"
                    ),
                    comment,
                ),
                (
                    _NFT_FILTER_FAMILY,
                    _NFT_FILTER_TABLE,
                    "forward",
                    (
                        f"ip daddr {guest_ip}/32 tcp dport {guest_port} "
                        "ct state new,related,established counter accept"
                    ),
                    comment,
                ),
            ]
        )

    async def async_setup_local_port_forward(
        self,
        vm_id: str,
        guest_ip: str,
        host_port: int,
        guest_port: int,
    ) -> None:
        """Expose localhost:host_port to guest_ip:guest_port (async)."""
        if not vm_id:
            raise ValueError("vm_id cannot be empty")
        if not guest_ip:
            raise ValueError("guest_ip cannot be empty")
        if host_port < 1 or host_port > 65535:
            raise ValueError("host_port must be 1-65535")
        if guest_port < 1 or guest_port > 65535:
            raise ValueError("guest_port must be 1-65535")

        await self.async_enable_ip_forwarding()
        await self._async_ensure_nftables_base()

        comment = f"smolvm:{vm_id}:local:{host_port}:{guest_port}"
        target = f"{guest_ip}:{guest_port}"

        await self._async_add_nft_rules_if_missing(
            [
                (
                    _NFT_NAT_FAMILY,
                    _NFT_NAT_TABLE,
                    "output",
                    f"ip daddr 127.0.0.1/32 tcp dport {host_port} counter dnat to {target}",
                    comment,
                ),
                (
                    _NFT_NAT_FAMILY,
                    _NFT_NAT_TABLE,
                    "postrouting",
                    (
                        f"ip saddr 127.0.0.0/8 ip daddr {guest_ip}/32 "
                        f"tcp dport {guest_port} counter snat to {self.host_ip}"
                    ),
                    comment,
                ),
                (
                    _NFT_FILTER_FAMILY,
                    _NFT_FILTER_TABLE,
                    "forward",
                    (
                        f"ip daddr {guest_ip}/32 tcp dport {guest_port} "
                        "ct state new,related,established counter accept"
                    ),
                    comment,
                ),
            ]
        )

    def cleanup_local_port_forward(
        self,
        vm_id: str,
        guest_ip: str,
        host_port: int,
        guest_port: int,
    ) -> None:
        """Remove localhost-only forwarding rules for one mapping."""
        if not vm_id:
            raise ValueError("vm_id cannot be empty")
        if not guest_ip:
            raise ValueError("guest_ip cannot be empty")
        if host_port < 1 or host_port > 65535:
            raise ValueError("host_port must be 1-65535")
        if guest_port < 1 or guest_port > 65535:
            raise ValueError("guest_port must be 1-65535")

        comment = f"smolvm:{vm_id}:local:{host_port}:{guest_port}"

        self._delete_nft_rules(_NFT_NAT_FAMILY, _NFT_NAT_TABLE, comment=comment)
        self._delete_nft_rules(_NFT_FILTER_FAMILY, _NFT_FILTER_TABLE, comment=comment)

    async def async_cleanup_local_port_forward(
        self,
        vm_id: str,
        guest_ip: str,
        host_port: int,
        guest_port: int,
    ) -> None:
        """Remove localhost-only forwarding rules for one mapping (async)."""
        if not vm_id:
            raise ValueError("vm_id cannot be empty")
        if not guest_ip:
            raise ValueError("guest_ip cannot be empty")
        if host_port < 1 or host_port > 65535:
            raise ValueError("host_port must be 1-65535")
        if guest_port < 1 or guest_port > 65535:
            raise ValueError("guest_port must be 1-65535")

        comment = f"smolvm:{vm_id}:local:{host_port}:{guest_port}"

        await self._async_delete_nft_rules(_NFT_NAT_FAMILY, _NFT_NAT_TABLE, comment=comment)
        await self._async_delete_nft_rules(_NFT_FILTER_FAMILY, _NFT_FILTER_TABLE, comment=comment)

    def cleanup_all_local_port_forwards(self, vm_id: str) -> None:
        """Best-effort cleanup for all localhost forwards belonging to vm_id."""
        if not vm_id:
            raise ValueError("vm_id cannot be empty")

        prefix = f"smolvm:{vm_id}:local:"

        self._delete_nft_rules(
            _NFT_NAT_FAMILY,
            _NFT_NAT_TABLE,
            comment_prefix=prefix,
        )
        self._delete_nft_rules(
            _NFT_FILTER_FAMILY,
            _NFT_FILTER_TABLE,
            comment_prefix=prefix,
        )

    async def async_cleanup_all_local_port_forwards(self, vm_id: str) -> None:
        """Best-effort cleanup for all localhost forwards belonging to vm_id (async)."""
        if not vm_id:
            raise ValueError("vm_id cannot be empty")

        prefix = f"smolvm:{vm_id}:local:"

        await self._async_delete_nft_rules(
            _NFT_NAT_FAMILY,
            _NFT_NAT_TABLE,
            comment_prefix=prefix,
        )
        await self._async_delete_nft_rules(
            _NFT_FILTER_FAMILY,
            _NFT_FILTER_TABLE,
            comment_prefix=prefix,
        )

    def cleanup_nat_rules(self, tap_name: str) -> None:
        """Remove per-TAP forward rule (global NAT rules stay shared)."""
        if not tap_name:
            raise ValueError("tap_name cannot be empty")

        iface = self.outbound_interface
        comment = f"smolvm:nat:tap:{tap_name}:to:{iface}"
        self._delete_nft_rules(
            _NFT_FILTER_FAMILY,
            _NFT_FILTER_TABLE,
            comment=comment,
        )

    async def async_cleanup_nat_rules(self, tap_name: str) -> None:
        """Remove per-TAP forward rule (global NAT rules stay shared) (async)."""
        if not tap_name:
            raise ValueError("tap_name cannot be empty")

        if self._outbound_interface is None:
            self._outbound_interface = await self._async_detect_outbound_interface()
        iface = self._outbound_interface

        comment = f"smolvm:nat:tap:{tap_name}:to:{iface}"
        await self._async_delete_nft_rules(
            _NFT_FILTER_FAMILY,
            _NFT_FILTER_TABLE,
            comment=comment,
        )

    def apply_egress_allowlist(
        self,
        tap_device: str,
        allowed_ips: list[str],
    ) -> None:
        """Restrict outbound traffic from *tap_device* to *allowed_ips* only.

        Installs per-TAP rules in the SmolVM filter forward chain keyed by tap
        name so they are isolated between tenants::

            # pass matching return traffic (established sessions)
            iifname <tap> ct state established,related counter accept
            # allow the configured destination set
            iifname <tap> ip daddr { <ip1>, <ip2>, ... } counter accept
            # drop everything else going out from this tap
            iifname <tap> ip daddr != { <ip1>, <ip2>, ... } counter drop

        The function is fail-closed and update-safe: it applies a single nft
        transaction that stages new rules first, then removes stale rules and
        any generic per-TAP NAT accept rule. If the transaction fails, old rules
        remain in place unchanged.

        Args:
            tap_device: TAP interface name (e.g., ``tap42``).
            allowed_ips: CIDR or host addresses that the guest may reach.
                Pass an empty list to deny *all* outbound IP traffic.

        Raises:
            ValueError: If ``tap_device`` is empty.
            NetworkError: If the nft call fails.
        """
        if not tap_device:
            raise ValueError("tap_device cannot be empty")

        logger.info(
            "Applying egress allowlist for %s: %s",
            tap_device,
            allowed_ips or "<deny all>",
        )

        self._ensure_nftables_base()
        iface = self.outbound_interface

        comment_prefix = f"smolvm:egress:{tap_device}"
        old_egress_delete_lines = self._find_nft_delete_rule_lines(
            _NFT_FILTER_FAMILY,
            _NFT_FILTER_TABLE,
            comment_prefix=f"{comment_prefix}:",
        )
        old_nat_accept_delete_lines = self._find_nft_delete_rule_lines(
            _NFT_FILTER_FAMILY,
            _NFT_FILTER_TABLE,
            comment=f"smolvm:nat:tap:{tap_device}:to:{iface}",
        )
        script_lines = [
            (
                f"add rule {_NFT_FILTER_FAMILY} {_NFT_FILTER_TABLE} forward "
                f"iifname {self._quote(tap_device)} ct state established,related "
                f"counter accept comment {self._quote(f'{comment_prefix}:established')}"
            ),
        ]

        if allowed_ips:
            # nftables anonymous set: ip daddr != { a, b, c }
            ip_set = ", ".join(allowed_ips)
            script_lines.append(
                (
                    f"add rule {_NFT_FILTER_FAMILY} {_NFT_FILTER_TABLE} forward "
                    f"iifname {self._quote(tap_device)} ip daddr {{ {ip_set} }} "
                    f"counter accept comment {self._quote(f'{comment_prefix}:allow')}"
                ),
            )
            drop_expr = (
                f"iifname {self._quote(tap_device)} "
                f"ip daddr != {{ {ip_set} }} counter drop"
            )
        else:
            # No IPs allowed — drop unconditionally.
            drop_expr = f"iifname {self._quote(tap_device)} counter drop"

        script_lines.append(

                f"add rule {_NFT_FILTER_FAMILY} {_NFT_FILTER_TABLE} forward "
                f"{drop_expr} comment {self._quote(f'{comment_prefix}:drop')}"

        )

        script_lines.extend(old_egress_delete_lines)
        script_lines.extend(old_nat_accept_delete_lines)

        self._run_nft_script("\n".join(script_lines) + "\n")

    async def async_apply_egress_allowlist(
        self,
        tap_device: str,
        allowed_ips: list[str],
    ) -> None:
        """Restrict outbound traffic from *tap_device* to *allowed_ips* only (async).

        See :meth:`apply_egress_allowlist` for full documentation.
        """
        if not tap_device:
            raise ValueError("tap_device cannot be empty")

        logger.info(
            "Applying egress allowlist for %s: %s",
            tap_device,
            allowed_ips or "<deny all>",
        )

        await self._async_ensure_nftables_base()

        if self._outbound_interface is None:
            self._outbound_interface = await self._async_detect_outbound_interface()
        iface = self._outbound_interface

        comment_prefix = f"smolvm:egress:{tap_device}"
        old_egress_delete_lines = await self._async_find_nft_delete_rule_lines(
            _NFT_FILTER_FAMILY,
            _NFT_FILTER_TABLE,
            comment_prefix=f"{comment_prefix}:",
        )
        old_nat_accept_delete_lines = await self._async_find_nft_delete_rule_lines(
            _NFT_FILTER_FAMILY,
            _NFT_FILTER_TABLE,
            comment=f"smolvm:nat:tap:{tap_device}:to:{iface}",
        )
        script_lines = [
            (
                f"add rule {_NFT_FILTER_FAMILY} {_NFT_FILTER_TABLE} forward "
                f"iifname {self._quote(tap_device)} ct state established,related "
                f"counter accept comment {self._quote(f'{comment_prefix}:established')}"
            ),
        ]

        if allowed_ips:
            ip_set = ", ".join(allowed_ips)
            script_lines.append(
                (
                    f"add rule {_NFT_FILTER_FAMILY} {_NFT_FILTER_TABLE} forward "
                    f"iifname {self._quote(tap_device)} ip daddr {{ {ip_set} }} "
                    f"counter accept comment {self._quote(f'{comment_prefix}:allow')}"
                ),
            )
            drop_expr = (
                f"iifname {self._quote(tap_device)} "
                f"ip daddr != {{ {ip_set} }} counter drop"
            )
        else:
            drop_expr = f"iifname {self._quote(tap_device)} counter drop"

        script_lines.append(

                f"add rule {_NFT_FILTER_FAMILY} {_NFT_FILTER_TABLE} forward "
                f"{drop_expr} comment {self._quote(f'{comment_prefix}:drop')}"

        )

        script_lines.extend(old_egress_delete_lines)
        script_lines.extend(old_nat_accept_delete_lines)

        await self._async_run_nft_script("\n".join(script_lines) + "\n")

    def remove_egress_rules(self, tap_device: str) -> None:
        """Remove all egress allowlist rules for *tap_device*.

        Must be called **before** ``vm.delete()`` to prevent a rule-table leak.
        nftables rules survive VM termination; this cleans them up atomically
        using a comment-prefix match.

        The call is best-effort: if the table no longer exists (e.g., host
        reboot) the function returns silently.

        Args:
            tap_device: TAP interface name used in :meth:`apply_egress_allowlist`.

        Raises:
            ValueError: If ``tap_device`` is empty.
        """
        if not tap_device:
            raise ValueError("tap_device cannot be empty")

        logger.info("Removing egress rules for %s", tap_device)

        self._delete_nft_rules(
            _NFT_FILTER_FAMILY,
            _NFT_FILTER_TABLE,
            comment_prefix=f"smolvm:egress:{tap_device}:",
        )

    async def async_remove_egress_rules(self, tap_device: str) -> None:
        """Remove all egress allowlist rules for *tap_device* (async).

        See :meth:`remove_egress_rules` for full documentation.
        """
        if not tap_device:
            raise ValueError("tap_device cannot be empty")

        logger.info("Removing egress rules for %s", tap_device)

        await self._async_delete_nft_rules(
            _NFT_FILTER_FAMILY,
            _NFT_FILTER_TABLE,
            comment_prefix=f"smolvm:egress:{tap_device}:",
        )

    def generate_mac(self, vm_number: int) -> str:
        """Generate deterministic VM MAC address for vm_number in [0, 65534]."""
        if vm_number < 0 or vm_number > 65534:
            raise ValueError("vm_number must be between 0 and 65534")
        return f"AA:FC:00:00:{(vm_number >> 8) & 0xFF:02X}:{vm_number & 0xFF:02X}"


def _extract_hostname(entry: str) -> str:
    """Extract hostname from a URL or bare domain string."""
    if "://" in entry:
        return urlparse(entry).hostname or entry
    # Bare domain — may include a port like "example.com:8080"
    return entry.split(":")[0]


def resolve_domains_to_ips(domains: list[str]) -> list[str]:
    """Resolve a list of domain entries to unique IP addresses.

    Each entry can be a full URL (``https://example.com/path``) or a bare
    hostname (``example.com``).  The wildcard ``"*"`` is skipped.

    Returns:
        Deduplicated list of resolved IP address strings.
    """
    seen: set[str] = set()
    result: list[str] = []

    for entry in domains:
        if entry == "*":
            continue

        hostname = _extract_hostname(entry)
        try:
            infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            logger.warning("Could not resolve hostname %r — skipping", hostname)
            continue

        for family, _type, _proto, _canonname, sockaddr in infos:
            if family != socket.AF_INET:
                continue
            ip = sockaddr[0]
            if ip not in seen:
                seen.add(ip)
                result.append(ip)

    return result


def check_network_prerequisites() -> list[str]:
    """Validate required host networking binaries and sudo access."""
    errors: list[str] = []

    for binary in ["ip", "nft"]:
        try:
            run_command(["which", binary], use_sudo=False)
        except SmolVMError:
            errors.append(f"'{binary}' command not found")

    if os.geteuid() != 0:
        checks = [
            (["ip", "link", "show"], "sudo ip"),
            (["nft", "list", "tables"], "sudo nft"),
            (["sysctl", "net.ipv4.ip_forward"], "sudo sysctl"),
        ]
        for cmd, label in checks:
            try:
                run_command(cmd, use_sudo=True)
            except SmolVMError:
                errors.append(f"{label} missing (run `smolvm setup`)")

    return errors
