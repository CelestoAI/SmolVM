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

"""Network management for SmolVM.

Handles TAP device creation, NAT rules, and cleanup.
Requires root/sudo privileges for network operations.
"""

import logging
import os
import shlex
from contextlib import suppress

from smolvm.exceptions import NetworkError, SmolVMError
from smolvm.utils import run_command

logger = logging.getLogger(__name__)

# Default network configuration
DEFAULT_HOST_IP = "172.16.0.1"
DEFAULT_NETMASK = "24"


class NetworkManager:
    """Manages network resources for VMs.

    Handles TAP devices and iptables NAT rules.
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

        # Flush existing addresses
        with suppress(NetworkError):
            run_command(["ip", "addr", "flush", "dev", tap_name])

        # Add IP address
        try:
            run_command(["ip", "addr", "add", f"{host_ip}/{netmask}", "dev", tap_name])
        except NetworkError as e:
            if "EEXIST" not in str(e):
                raise

        # Bring interface up
        run_command(["ip", "link", "set", tap_name, "up"])

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
        try:
            with open("/proc/sys/net/ipv4/ip_forward", "w") as f:
                f.write("1")
        except PermissionError:
            run_command(
                ["sysctl", "-w", "net.ipv4.ip_forward=1"],
                use_sudo=True,
            )

    def setup_nat(self, tap_name: str) -> None:
        """Set up NAT rules for a TAP device.

        Creates:
        - MASQUERADE rule for outbound traffic
        - FORWARD rules for traffic flow
        - Inter-VM isolation rule

        Args:
            tap_name: Name of the TAP device.

        Raises:
            NetworkError: If rule creation fails.
        """
        if not tap_name:
            raise ValueError("tap_name cannot be empty")

        logger.info("Setting up NAT for TAP: %s", tap_name)

        iface = self.outbound_interface

        # Enable IP forwarding
        self.enable_ip_forwarding()

        # MASQUERADE for outbound (idempotent check)
        if not self._rule_exists("nat", "POSTROUTING", ["-o", iface, "-j", "MASQUERADE"]):
            run_command(
                [
                    "iptables",
                    "-t",
                    "nat",
                    "-A",
                    "POSTROUTING",
                    "-o",
                    iface,
                    "-j",
                    "MASQUERADE",
                ]
            )

        # Allow established/related connections
        if not self._rule_exists(
            "filter",
            "FORWARD",
            ["-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
        ):
            run_command(
                [
                    "iptables",
                    "-A",
                    "FORWARD",
                    "-m",
                    "conntrack",
                    "--ctstate",
                    "RELATED,ESTABLISHED",
                    "-j",
                    "ACCEPT",
                ]
            )

        # Allow TAP to outbound interface
        if not self._rule_exists(
            "filter", "FORWARD", ["-i", tap_name, "-o", iface, "-j", "ACCEPT"]
        ):
            run_command(
                [
                    "iptables",
                    "-A",
                    "FORWARD",
                    "-i",
                    tap_name,
                    "-o",
                    iface,
                    "-j",
                    "ACCEPT",
                ]
            )

        # Block inter-VM traffic (security)
        if not self._rule_exists("filter", "FORWARD", ["-i", "tap+", "-o", "tap+", "-j", "DROP"]):
            run_command(
                [
                    "iptables",
                    "-A",
                    "FORWARD",
                    "-i",
                    "tap+",
                    "-o",
                    "tap+",
                    "-j",
                    "DROP",
                ]
            )

    def setup_ssh_port_forward(
        self,
        vm_id: str,
        guest_ip: str,
        host_port: int,
        guest_port: int = 22,
    ) -> None:
        """Set up inbound SSH port forwarding for a VM.

        Creates rules to forward:
        - host:<host_port> on outbound interface -> guest_ip:<guest_port>
        - localhost:<host_port> -> guest_ip:<guest_port> (host-local access)

        Args:
            vm_id: VM identifier (used in rule comments).
            guest_ip: Guest IP address.
            host_port: Host TCP port exposed for SSH.
            guest_port: Guest SSH port (default: 22).
        """
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

        prerouting = [
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
            target,
        ]
        if not self._rule_exists("nat", "PREROUTING", prerouting):
            run_command(["iptables", "-t", "nat", "-A", "PREROUTING", *prerouting])

        output = [
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
            target,
        ]
        if not self._rule_exists("nat", "OUTPUT", output):
            run_command(["iptables", "-t", "nat", "-A", "OUTPUT", *output])

        forward = [
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
        ]
        if not self._rule_exists("filter", "FORWARD", forward):
            run_command(["iptables", "-A", "FORWARD", *forward])

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
        if host_port < 1 or host_port > 65535:
            raise ValueError("host_port must be 1-65535")
        if guest_port < 1 or guest_port > 65535:
            raise ValueError("guest_port must be 1-65535")

        iface = self.outbound_interface
        target = f"{guest_ip}:{guest_port}"
        comment = f"smolvm:{vm_id}:ssh"

        with suppress(NetworkError):
            run_command(
                [
                    "iptables",
                    "-D",
                    "FORWARD",
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
                ]
            )

        with suppress(NetworkError):
            run_command(
                [
                    "iptables",
                    "-t",
                    "nat",
                    "-D",
                    "OUTPUT",
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
                    target,
                ]
            )

        with suppress(NetworkError):
            run_command(
                [
                    "iptables",
                    "-t",
                    "nat",
                    "-D",
                    "PREROUTING",
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
                    target,
                ]
            )

    def setup_local_port_forward(
        self,
        vm_id: str,
        guest_ip: str,
        host_port: int,
        guest_port: int,
    ) -> None:
        """Set up localhost-only TCP forwarding from host to guest.

        Creates rules to forward:
        - 127.0.0.1:<host_port> -> guest_ip:<guest_port>

        This does not add a PREROUTING rule, so the service is not exposed on
        external host interfaces.
        """
        if not vm_id:
            raise ValueError("vm_id cannot be empty")
        if not guest_ip:
            raise ValueError("guest_ip cannot be empty")
        if host_port < 1 or host_port > 65535:
            raise ValueError("host_port must be 1-65535")
        if guest_port < 1 or guest_port > 65535:
            raise ValueError("guest_port must be 1-65535")

        self.enable_ip_forwarding()
        target = f"{guest_ip}:{guest_port}"
        comment = f"smolvm:{vm_id}:local:{host_port}:{guest_port}"

        output = [
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
            target,
        ]
        if not self._rule_exists("nat", "OUTPUT", output):
            run_command(["iptables", "-t", "nat", "-A", "OUTPUT", *output])

        forward = [
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
        ]
        if not self._rule_exists("filter", "FORWARD", forward):
            run_command(["iptables", "-A", "FORWARD", *forward])

    def cleanup_local_port_forward(
        self,
        vm_id: str,
        guest_ip: str,
        host_port: int,
        guest_port: int,
    ) -> None:
        """Remove localhost-only TCP forwarding rules for a VM."""
        if not vm_id:
            raise ValueError("vm_id cannot be empty")
        if not guest_ip:
            raise ValueError("guest_ip cannot be empty")
        if host_port < 1 or host_port > 65535:
            raise ValueError("host_port must be 1-65535")
        if guest_port < 1 or guest_port > 65535:
            raise ValueError("guest_port must be 1-65535")

        target = f"{guest_ip}:{guest_port}"
        comment = f"smolvm:{vm_id}:local:{host_port}:{guest_port}"

        with suppress(NetworkError, SmolVMError):
            run_command(
                [
                    "iptables",
                    "-D",
                    "FORWARD",
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
                ]
            )

        with suppress(NetworkError, SmolVMError):
            run_command(
                [
                    "iptables",
                    "-t",
                    "nat",
                    "-D",
                    "OUTPUT",
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
                    target,
                ]
            )

    def cleanup_all_local_port_forwards(self, vm_id: str) -> None:
        """Best-effort removal of all localhost-only forwarding rules for a VM."""
        if not vm_id:
            raise ValueError("vm_id cannot be empty")

        comment_prefix = f"smolvm:{vm_id}:local:"

        for table, chain in (("nat", "OUTPUT"), ("filter", "FORWARD")):
            for rule_tokens in self._list_chain_rules(table, chain):
                comment = self._extract_comment(rule_tokens)
                if comment is None or not comment.startswith(comment_prefix):
                    continue

                delete_tokens = list(rule_tokens)
                if not delete_tokens or delete_tokens[0] != "-A":
                    continue
                delete_tokens[0] = "-D"

                cmd = ["iptables"]
                if table != "filter":
                    cmd.extend(["-t", table])
                cmd.extend(delete_tokens)

                with suppress(NetworkError, SmolVMError):
                    run_command(cmd)

    def _list_chain_rules(self, table: str, chain: str) -> list[list[str]]:
        """Return parsed `iptables -S <chain>` rules for a table."""
        cmd = ["iptables"]
        if table != "filter":
            cmd.extend(["-t", table])
        cmd.extend(["-S", chain])

        result = run_command(cmd)
        rules: list[list[str]] = []
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not stripped.startswith("-A "):
                continue
            try:
                tokens = shlex.split(stripped)
            except ValueError:
                continue
            if len(tokens) >= 2 and tokens[0] == "-A" and tokens[1] == chain:
                rules.append(tokens)
        return rules

    @staticmethod
    def _extract_comment(rule_tokens: list[str]) -> str | None:
        """Extract `--comment` value from parsed iptables tokens."""
        for i, token in enumerate(rule_tokens):
            if token == "--comment" and i + 1 < len(rule_tokens):
                return rule_tokens[i + 1]
        return None

    def _rule_exists(self, table: str, chain: str, rule_parts: list[str]) -> bool:
        """Check if an iptables rule already exists.

        Args:
            table: Table name (nat, filter).
            chain: Chain name (POSTROUTING, FORWARD).
            rule_parts: Rule specification parts.

        Returns:
            True if rule exists.
        """
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
        """Delete a TAP device.

        Args:
            tap_name: Name of the TAP device.
        """
        if not tap_name:
            raise ValueError("tap_name cannot be empty")

        logger.info("Cleaning up TAP device: %s", tap_name)

        try:
            run_command(["ip", "link", "delete", tap_name])
        except NetworkError as e:
            if "Cannot find device" not in str(e):
                logger.warning("Failed to delete TAP %s: %s", tap_name, e)

    def cleanup_nat_rules(self, tap_name: str) -> None:
        """Remove NAT rules for a specific TAP device.

        Args:
            tap_name: Name of the TAP device.
        """
        if not tap_name:
            raise ValueError("tap_name cannot be empty")

        logger.info("Cleaning up NAT rules for TAP: %s", tap_name)

        iface = self.outbound_interface

        # Remove TAP-specific forward rule
        with suppress(NetworkError):
            run_command(
                [
                    "iptables",
                    "-D",
                    "FORWARD",
                    "-i",
                    tap_name,
                    "-o",
                    iface,
                    "-j",
                    "ACCEPT",
                ]
            )

    def generate_mac(self, vm_number: int) -> str:
        """Generate a MAC address for a VM.

        Args:
            vm_number: Unique number for the VM.

        Returns:
            MAC address string (e.g., "AA:FC:00:00:00:01").
        """
        if vm_number < 0 or vm_number > 255:
            raise ValueError("vm_number must be between 0 and 255")

        return f"AA:FC:00:00:00:{vm_number:02X}"


def check_network_prerequisites() -> list[str]:
    """Check if network prerequisites are met.

    Returns:
        List of error messages (empty if all good).
    """
    errors = []

    # Check for ip command
    try:
        run_command(["which", "ip"], use_sudo=False)
    except SmolVMError:
        errors.append("'ip' command not found (install iproute2)")

    # Check for iptables
    try:
        run_command(["which", "iptables"], use_sudo=False)
    except SmolVMError:
        errors.append("'iptables' command not found")

    # Check for non-interactive sudo access for required runtime commands.
    if os.geteuid() != 0:
        privileged_checks: list[tuple[list[str], str]] = [
            (["ip", "link", "show"], "non-interactive sudo for 'ip' runtime commands"),
            (["iptables", "-L"], "non-interactive sudo for 'iptables' runtime commands"),
            (
                ["sysctl", "net.ipv4.ip_forward"],
                "non-interactive sudo for 'sysctl' runtime commands",
            ),
        ]
        for cmd, label in privileged_checks:
            try:
                run_command(cmd, use_sudo=True)
            except SmolVMError:
                errors.append(
                    f"{label} is not configured "
                    "(run: sudo ./scripts/system-setup.sh --configure-runtime)"
                )

    return errors
