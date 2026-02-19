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

import logging
import os
import shlex
from contextlib import suppress
from pathlib import Path

from smolvm.exceptions import NetworkError, SmolVMError
from smolvm.utils import run_command

logger = logging.getLogger(__name__)

# Default network configuration
DEFAULT_HOST_IP = "172.16.0.1"
DEFAULT_NETMASK = "24"


class NetworkManager:
    """Manages network resources for VMs.

    Handles TAP devices and iptables NAT rules.
    Optimized to use batched subprocess calls (ip -batch, iptables-restore)
    to minimize fork overhead and lock contention.
    """

    def __init__(self, host_ip: str = DEFAULT_HOST_IP) -> None:
        """Initialize the network manager.

        Args:
            host_ip: IP address for the host side of TAP devices.
        """
        if not host_ip:
            raise ValueError("host_ip cannot be empty")

        self.host_ip = host_ip
        self._outbound_interface: str | None = None

    @property
    def outbound_interface(self) -> str:
        """Get the default outbound network interface."""
        if self._outbound_interface is None:
            self._outbound_interface = self._detect_outbound_interface()
        return self._outbound_interface

    def _detect_outbound_interface(self) -> str:
        """Detect the default outbound network interface.

        Returns:
            Interface name (e.g., "eth0", "ens4").

        Raises:
            NetworkError: If no default route found.
        """
        try:
            result = run_command(
                ["ip", "route", "show", "default"],
                use_sudo=False,
            )
            # Parse: "default via X.X.X.X dev eth0 ..."
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

    def create_tap(self, tap_name: str, user: str | None = None) -> None:
        """Create a TAP device.

        Args:
            tap_name: Name of the TAP device (e.g., "tap1").
            user: Owner user (defaults to current user).

        Raises:
            NetworkError: If creation fails.
        """
        if not tap_name:
            raise ValueError("tap_name cannot be empty")

        if user is None:
            user = os.environ.get("USER", "root")

        logger.info("Creating TAP device: %s (user: %s)", tap_name, user)

        # Create TAP device (ignore if exists)
        try:
            run_command(
                ["ip", "tuntap", "add", tap_name, "mode", "tap", "user", user],
            )
        except SmolVMError as e:
            if "File exists" in str(e) or "EEXIST" in str(e):
                logger.debug("TAP device %s already exists", tap_name)
            else:
                raise

    def configure_tap(
        self,
        tap_name: str,
        host_ip: str | None = None,
        netmask: str = DEFAULT_NETMASK,
    ) -> None:
        """Configure a TAP device with IP and bring it up.

        Uses ip -batch to minimize subprocess calls.

        Args:
            tap_name: Name of the TAP device.
            host_ip: IP address to assign (defaults to self.host_ip).
            netmask: Network mask in CIDR notation.

        Raises:
            NetworkError: If configuration fails.
        """
        if not tap_name:
            raise ValueError("tap_name cannot be empty")

        if host_ip is None:
            host_ip = self.host_ip

        logger.info("Configuring TAP %s with IP %s/%s", tap_name, host_ip, netmask)

        # Build batch commands
        # 1. flush addr
        # 2. add addr
        # 3. set up
        batch = [
            f"addr flush dev {tap_name}",
            f"addr add {host_ip}/{netmask} dev {tap_name}",
            f"link set {tap_name} up",
        ]

        try:
            self._run_ip_batch(batch)
        except Exception as e:
            # EEXIST is fine for addr add, but difficult to catch in batch.
            # However, we flushed first, so EEXIST shouldn't happen unless race.
            if "RTNETLINK answers: File exists" in str(e):
                pass
            else:
                raise

        # Enable route_localnet to allow localhost forwarding
        # This is a sysctl generic write
        self._write_sysctl(f"net/ipv4/conf/{tap_name}/route_localnet", "1")

    def add_route(self, ip_address: str, device: str) -> None:
        """Add a static route for a specific IP via a device.

        Args:
            ip_address: Target IP (e.g. "172.16.0.2").
            device: Output device name.

        Raises:
            NetworkError: If route addition fails.
        """
        if not ip_address:
            raise ValueError("ip_address cannot be empty")
        if not device:
            raise ValueError("device cannot be empty")

        logger.info("Adding route: %s via %s", ip_address, device)
        try:
            run_command(["ip", "route", "add", f"{ip_address}/32", "dev", device])
        except NetworkError as e:
            if "File exists" not in str(e):
                raise

    def enable_ip_forwarding(self) -> None:
        """Enable IP forwarding on the host."""
        logger.debug("Enabling IP forwarding")
        self._write_sysctl("net/ipv4/ip_forward", "1")

    def _write_sysctl(self, key_path: str, value: str) -> None:
        """Write a value to /proc/sys efficiently."""
        # Try direct file write first (works if likely permissions match)
        path = Path(f"/proc/sys/{key_path}")
        try:
            path.write_text(value)
            return
        except (PermissionError, FileNotFoundError):
            pass

        # Fallback to sudo sysctl
        # dotted key
        key = key_path.replace("/", ".")
        try:
            run_command(["sysctl", "-w", f"{key}={value}"], use_sudo=True)
        except Exception as e:
            logger.warning("Failed to set sysctl %s: %s", key, e)

    def _run_ip_batch(self, commands: list[str]) -> None:
        """Run a batch of ip commands via 'ip -batch -'."""
        if not commands:
            return
        input_str = "\n".join(commands)
        run_command(["ip", "-batch", "-"], input=input_str, use_sudo=True)

    def _apply_iptables_restore(self, table: str, rules_to_append: list[str]) -> None:
        """Apply a set of rules using iptables-restore.

        This fetches the current rules (iptables-save), checks for existence
        of new rules to avoid duplicates, and then applies new ones atomically.
        """
        if not rules_to_append:
            return

        # 1. Get current state to avoid duplicates
        # We need this because iptables-restore --noflush appends duplicates
        existing_rules = self._get_table_rules(table)

        # 2. Filter out rules that already exist
        new_rules = []
        for rule in rules_to_append:
            # Simple string matching.
            # We assume the constructed rule matches iptables-save output format enough.
            # This is heuristics-based but standard flags order usually matches.
            # The most robust way is strict token parsing, but string-contains is often sufficient
            # if we are consistent.
            # We check if the rule (e.g. "-A POSTROUTING ...") appears in existing lines.

            # Note: existing_rules contains full lines like "-A POSTROUTING -s 1.2.3.4 ..."
            if rule not in existing_rules:
                new_rules.append(rule)

        if not new_rules:
            return

        # 3. Construct blob
        lines = [f"*{table}"]
        lines.extend(new_rules)
        lines.append("COMMIT")
        lines.append("")  # trailing newline

        blob = "\n".join(lines)

        # 4. Apply
        run_command(["iptables-restore", "--noflush"], input=blob, use_sudo=True)

    def _get_table_rules(self, table: str) -> set[str]:
        """Get all current rules for a table as a set of logical lines."""
        try:
            res = run_command(["iptables-save", "-t", table], use_sudo=True)
            return set(line.strip() for line in res.stdout.splitlines() if line.startswith("-A"))
        except Exception:
            return set()

    def setup_nat(self, tap_name: str) -> None:
        """Set up NAT rules for a TAP device.

        Uses batched iptables-restore.
        """
        if not tap_name:
            raise ValueError("tap_name cannot be empty")

        logger.info("Setting up NAT for TAP: %s", tap_name)

        iface = self.outbound_interface

        # Enable IP forwarding
        self.enable_ip_forwarding()

        # Build rules list
        # We target the 'nat' table and 'filter' table separately or together?
        # iptables-restore can handle multiple tables in one blob.
        # But our _apply helper handles one. Let's do two commits if needed.

        # NAT Table Rules
        nat_rules = []
        # MASQUERADE for outbound
        nat_rules.append(f"-A POSTROUTING -o {iface} -j MASQUERADE")

        self._apply_iptables_restore("nat", nat_rules)

        # Filter Table Rules
        filter_rules = []
        # Allow established/related
        filter_rules.append("-A FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT")
        # Allow TAP to outbound
        filter_rules.append(f"-A FORWARD -i {tap_name} -o {iface} -j ACCEPT")
        # Block inter-VM traffic (tap+ to tap+)
        filter_rules.append("-A FORWARD -i tap+ -o tap+ -j DROP")

        self._apply_iptables_restore("filter", filter_rules)

    def setup_ssh_port_forward(
        self,
        vm_id: str,
        guest_ip: str,
        host_port: int,
        guest_port: int = 22,
    ) -> None:
        """Set up inbound SSH port forwarding for a VM."""
        if not vm_id:
            raise ValueError("vm_id cannot be empty")
        if not guest_ip:
            raise ValueError("guest_ip cannot be empty")
        if host_port < 1 or host_port > 65535:
            raise ValueError("host_port must be 1-65535")
        if guest_port < 1 or guest_port > 65535:
            raise ValueError("guest_port must be 1-65535")

        self.enable_ip_forwarding()
        iface = self.outbound_interface
        target = f"{guest_ip}:{guest_port}"
        comment = f"smolvm:{vm_id}:ssh"

        # NAT Rules
        nat_rules = []
        # PREROUTING (Host Port -> Guest)
        nat_rules.append(
            f"-A PREROUTING -i {iface} -p tcp -m tcp --dport {host_port} -m comment --comment {comment} -j DNAT --to-destination {target}"
        )
        # OUTPUT (Localhost -> Guest)
        nat_rules.append(
            f"-A OUTPUT -d 127.0.0.1/32 -p tcp -m tcp --dport {host_port} -m comment --comment {comment} -j DNAT --to-destination {target}"
        )
        # SNAT (Localhost reply rewrite)
        nat_rules.append(
            f"-A POSTROUTING -s 127.0.0.0/8 -d {guest_ip}/32 -p tcp -m tcp --dport {guest_port} -m comment --comment {comment} -j SNAT --to-source {self.host_ip}"
        )
        self._apply_iptables_restore("nat", nat_rules)

        # Filter Rules
        filter_rules = []
        filter_rules.append(
            f"-A FORWARD -d {guest_ip}/32 -p tcp -m tcp --dport {guest_port} -m conntrack --ctstate NEW,RELATED,ESTABLISHED -m comment --comment {comment} -j ACCEPT"
        )
        self._apply_iptables_restore("filter", filter_rules)

    def cleanup_ssh_port_forward(
        self,
        vm_id: str,
        guest_ip: str,
        host_port: int,
        guest_port: int = 22,
    ) -> None:
        """Remove inbound SSH port-forwarding rules for a VM."""
        if not vm_id:
            raise ValueError("vm_id cannot be empty")
        if not guest_ip:
            raise ValueError("guest_ip cannot be empty")

        # Cleanup uses individual -D commands because iptables-restore doesn't support delete easily
        # without reloading the whole table state (which is race-prone if we manipulate it manually).
        # We reuse the existing logic but simplified helpers?
        # Actually, optimization for cleanup is less critical (async cleanup).
        # But we can still batch deletions if we know them?
        # No, "iptables -D" requires precise matching.

        # We used to use individual run_command calls.
        # We can keep that for cleanup, or implement batch deletion via iptables-restore (reload).
        # Reloading is risky.
        # Let's keep individual cleanup for safety, or use loop?

        # The benchmark showed 4 cleanup calls.
        # We can optimize cleanup later if needed. P95 spike in CREATE is the priority.
        # So I will revert to individual calls for cleanup to minimize risk.

        comment = f"smolvm:{vm_id}:ssh"
        iface = self.outbound_interface

        # We can batch the deletions into a script?
        # "iptables -D ..."
        # No, run_command takes one cmd.
        # We could write a small shell script and run it? No.

        # Revert to standard calls for cleanup.
        # Or define a _delete_rule helper.

        self._delete_rule(
            "nat",
            "POSTROUTING",
            [
                "-s",
                "127.0.0.0/8",
                "-d",
                guest_ip,
                "-p",
                "tcp",
                "--dport",
                str(guest_port),
                "-m",
                "comment",
                "--comment",
                comment,
                "-j",
                "SNAT",
                "--to-source",
                self.host_ip,
            ],
        )
        self._delete_rule(
            "filter",
            "FORWARD",
            [
                "-p",
                "tcp",
                "-d",
                guest_ip,
                "--dport",
                str(guest_port),
                "-m",
                "conntrack",
                "--ctstate",
                "NEW,ESTABLISHED,RELATED",
                "-m",
                "comment",
                "--comment",
                comment,
                "-j",
                "ACCEPT",
            ],
        )
        self._delete_rule(
            "nat",
            "OUTPUT",
            [
                "-d",
                "127.0.0.1/32",
                "-p",
                "tcp",
                "--dport",
                str(host_port),
                "-m",
                "comment",
                "--comment",
                comment,
                "-j",
                "DNAT",
                "--to-destination",
                f"{guest_ip}:{guest_port}",
            ],
        )
        self._delete_rule(
            "nat",
            "PREROUTING",
            [
                "-i",
                iface,
                "-p",
                "tcp",
                "--dport",
                str(host_port),
                "-m",
                "comment",
                "--comment",
                comment,
                "-j",
                "DNAT",
                "--to-destination",
                f"{guest_ip}:{guest_port}",
            ],
        )

    def setup_local_port_forward(
        self,
        vm_id: str,
        guest_ip: str,
        host_port: int,
        guest_port: int,
    ) -> None:
        """Set up localhost-only TCP forwarding."""
        if not vm_id:
            raise ValueError("vm_id")

        comment = f"smolvm:{vm_id}:local:{host_port}:{guest_port}"
        target = f"{guest_ip}:{guest_port}"

        nat_rules = []
        nat_rules.append(
            f"-A OUTPUT -d 127.0.0.1/32 -p tcp --dport {host_port} -m comment --comment {comment} -j DNAT --to-destination {target}"
        )
        nat_rules.append(
            f"-A POSTROUTING -s 127.0.0.0/8 -d {guest_ip}/32 -p tcp --dport {guest_port} -m comment --comment {comment} -j SNAT --to-source {self.host_ip}"
        )
        self._apply_iptables_restore("nat", nat_rules)

        filter_rules = []
        filter_rules.append(
            f"-A FORWARD -p tcp -d {guest_ip}/32 --dport {guest_port} -m conntrack --ctstate NEW,ESTABLISHED,RELATED -m comment --comment {comment} -j ACCEPT"
        )
        self._apply_iptables_restore("filter", filter_rules)

    def cleanup_local_port_forward(
        self,
        vm_id: str,
        guest_ip: str,
        host_port: int,
        guest_port: int,
    ) -> None:
        """Cleanup local port forward."""
        comment = f"smolvm:{vm_id}:local:{host_port}:{guest_port}"

        self._delete_rule(
            "nat",
            "POSTROUTING",
            [
                "-s",
                "127.0.0.0/8",
                "-d",
                guest_ip,
                "-p",
                "tcp",
                "--dport",
                str(guest_port),
                "-m",
                "comment",
                "--comment",
                comment,
                "-j",
                "SNAT",
                "--to-source",
                self.host_ip,
            ],
        )
        self._delete_rule(
            "filter",
            "FORWARD",
            [
                "-p",
                "tcp",
                "-d",
                guest_ip,
                "--dport",
                str(guest_port),
                "-m",
                "conntrack",
                "--ctstate",
                "NEW,ESTABLISHED,RELATED",
                "-m",
                "comment",
                "--comment",
                comment,
                "-j",
                "ACCEPT",
            ],
        )
        self._delete_rule(
            "nat",
            "OUTPUT",
            [
                "-d",
                "127.0.0.1/32",
                "-p",
                "tcp",
                "--dport",
                str(host_port),
                "-m",
                "comment",
                "--comment",
                comment,
                "-j",
                "DNAT",
                "--to-destination",
                f"{guest_ip}:{guest_port}",
            ],
        )

    def cleanup_all_local_port_forwards(self, vm_id: str) -> None:
        """Best-effort cleanup all local forwards."""
        # Using existing iterative logic is fine for cleanup
        # We can implement it using standard calls
        comment_prefix = f"smolvm:{vm_id}:local:"

        for table, chain in (("nat", "OUTPUT"), ("filter", "FORWARD")):
            for rule_tokens in self._list_chain_rules(table, chain):
                comment = self._extract_comment(rule_tokens)
                if comment and comment.startswith(comment_prefix):
                    # To delete, we need to reproduce the rule options
                    # rule_tokens contains ["-A", "CHAIN", ...]
                    # We need ["-D", "CHAIN", ...]
                    # slice [2:] gives args
                    args = rule_tokens[2:]
                    self._delete_rule(table, chain, args)

    def _delete_rule(self, table: str, chain: str, rule_parts: list[str]) -> None:
        """Delete a rule securely."""
        try:
            cmd = ["iptables"]
            if table != "filter":
                cmd.extend(["-t", table])
            cmd.extend(["-D", chain, *rule_parts])
            run_command(cmd)
        except (NetworkError, SmolVMError):
            pass

    def _list_chain_rules(self, table: str, chain: str) -> list[list[str]]:
        """Return parsed rules."""
        cmd = ["iptables"]
        if table != "filter":
            cmd.extend(["-t", table])
        cmd.extend(["-S", chain])

        try:
            result = run_command(cmd)
            rules = []
            for line in result.stdout.splitlines():
                if not line.startswith("-A "):
                    continue
                try:
                    tokens = shlex.split(line.strip())
                    if len(tokens) >= 2 and tokens[1] == chain:
                        rules.append(tokens)
                except ValueError:
                    continue
            return rules
        except Exception:
            return []

    @staticmethod
    def _extract_comment(rule_tokens: list[str]) -> str | None:
        for i, token in enumerate(rule_tokens):
            if token == "--comment" and i + 1 < len(rule_tokens):
                return rule_tokens[i + 1]
        return None

    def _rule_exists(self, table: str, chain: str, rule_parts: list[str]) -> bool:
        # Kept for legacy compatibility if needed, but not used in batched setup
        try:
            cmd = ["iptables"]
            if table != "filter":
                cmd.extend(["-t", table])
            cmd.extend(["-C", chain, *rule_parts])
            run_command(cmd, check=True)
            return True
        except SmolVMError:
            return False

    def cleanup_tap(self, tap_name: str) -> None:
        if not tap_name:
            raise ValueError("tap_name")
        logger.info("Cleaning up TAP device: %s", tap_name)
        try:
            run_command(["ip", "link", "delete", tap_name])
        except NetworkError as e:
            if "Cannot find device" not in str(e):
                logger.warning("Failed to delete TAP %s: %s", tap_name, e)

    def cleanup_nat_rules(self, tap_name: str) -> None:
        if not tap_name:
            raise ValueError("tap_name")
        logger.info("Cleaning up NAT rules for TAP: %s", tap_name)
        iface = self.outbound_interface
        self._delete_rule("filter", "FORWARD", ["-i", tap_name, "-o", iface, "-j", "ACCEPT"])

    def generate_mac(self, vm_number: int) -> str:
        if vm_number < 0 or vm_number > 255:
            raise ValueError("vm_number 0-255")
        return f"AA:FC:00:00:00:{vm_number:02X}"


# check_network_prerequisites remains mostly same
def check_network_prerequisites() -> list[str]:
    errors = []
    # Check for ip/iptables
    for binary in ["ip", "iptables"]:
        try:
            run_command(["which", binary], use_sudo=False)
        except SmolVMError:
            errors.append(f"'{binary}' command not found")

    if os.geteuid() != 0:
        # Check privileges
        checks = [
            (["ip", "link", "show"], "sudo ip"),
            (["iptables", "-L"], "sudo iptables"),
            # sysctl check might fail if we read. Check write access?
            # Just checking if we can run sudo sysctl
            (["sysctl", "-n", "net.ipv4.ip_forward"], "sudo sysctl"),
        ]
        for cmd, label in checks:
            try:
                run_command(cmd, use_sudo=True)
            except SmolVMError:
                errors.append(f"{label} missing (run setup script)")

    return errors
