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

"""Tests for SmolVM network module."""

from unittest.mock import MagicMock, patch

from smolvm.exceptions import SmolVMError
from smolvm.network import NetworkManager, check_network_prerequisites


def _collect_nft_scripts(mock_run_command: MagicMock) -> str:
    scripts: list[str] = []
    for call in mock_run_command.call_args_list:
        cmd = call.args[0]
        if cmd == ["nft", "-f", "-"]:
            scripts.append(call.kwargs.get("input", ""))
    return "\n".join(scripts)


class TestSSHPortForwarding:
    """Tests for SSH forwarding rule setup/cleanup."""

    @patch("smolvm.network.run_command")
    def test_setup_ssh_port_forward_adds_elements(self, mock_run_command: MagicMock) -> None:
        """Setup should add elements to DNAT maps and SNAT/forward sets."""
        mock_run_command.return_value = MagicMock(stdout="")

        nm = NetworkManager()
        nm._outbound_interface = "eth0"

        nm.setup_ssh_port_forward(vm_id="vm001", guest_ip="172.16.0.2", host_port=2200)

        scripts = _collect_nft_scripts(mock_run_command)
        # Maps/sets declared in base setup
        assert "add map ip smolvm_nat dnat_ext" in scripts
        assert "add map ip smolvm_nat dnat_local" in scripts
        assert "add set ip smolvm_nat snat_return" in scripts
        assert "add set inet smolvm_filter fwd_allow" in scripts
        # Static rules referencing maps/sets
        assert "dnat to tcp dport map @dnat_ext" in scripts
        assert "dnat to tcp dport map @dnat_local" in scripts
        # Per-VM elements
        assert "add element ip smolvm_nat dnat_ext { 2200 : 172.16.0.2 . 22 }" in scripts
        assert "add element ip smolvm_nat dnat_local { 2200 : 172.16.0.2 . 22 }" in scripts
        assert "add element ip smolvm_nat snat_return { 172.16.0.2 . 22 }" in scripts
        assert "add element inet smolvm_filter fwd_allow { 172.16.0.2 . 22 }" in scripts

    @patch("smolvm.network.run_command")
    def test_cleanup_ssh_port_forward_deletes_elements_and_legacy_rules(
        self, mock_run_command: MagicMock
    ) -> None:
        """Cleanup should delete elements and also try legacy comment-based rules."""

        def _side_effect(cmd: list[str], *args: object, **kwargs: object) -> MagicMock:
            # Legacy rule listing returns old-style rules
            if cmd == ["nft", "-a", "list", "table", "ip", "smolvm_nat"]:
                return MagicMock(
                    stdout=(
                        "table ip smolvm_nat {\n"
                        "  chain prerouting {\n"
                        '    tcp dport 2200 comment "smolvm:vm001:ssh" # handle 14\n'
                        "  }\n"
                        "}\n"
                    )
                )
            if cmd == ["nft", "-a", "list", "table", "inet", "smolvm_filter"]:
                return MagicMock(stdout="table inet smolvm_filter {\n}\n")
            return MagicMock(stdout="")

        mock_run_command.side_effect = _side_effect

        nm = NetworkManager()
        nm.cleanup_ssh_port_forward(vm_id="vm001", guest_ip="172.16.0.2", host_port=2200)

        scripts = _collect_nft_scripts(mock_run_command)
        # New: element deletes
        assert "delete element ip smolvm_nat dnat_ext { 2200 }" in scripts
        assert "delete element ip smolvm_nat dnat_local { 2200 }" in scripts
        assert "delete element ip smolvm_nat snat_return { 172.16.0.2 . 22 }" in scripts
        assert "delete element inet smolvm_filter fwd_allow { 172.16.0.2 . 22 }" in scripts
        # Legacy: comment-based rule deletes still run
        assert "delete rule ip smolvm_nat prerouting handle 14" in scripts


class TestTapManagement:
    """Tests for TAP device create behavior."""

    @patch("smolvm.network.run_command")
    def test_create_tap_is_idempotent_when_existing(self, mock_run_command: MagicMock) -> None:
        """'File exists' errors should be treated as success."""
        mock_run_command.side_effect = SmolVMError("RTNETLINK answers: File exists")

        nm = NetworkManager()
        nm.create_tap("tap42", "alice")

        mock_run_command.assert_called_once_with(
            ["ip", "tuntap", "add", "tap42", "mode", "tap", "user", "alice"]
        )

    @patch("smolvm.network.time.sleep")
    @patch("smolvm.network.run_command")
    def test_create_tap_retries_busy_then_succeeds(
        self, mock_run_command: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """Transient busy errors should be retried."""
        mock_run_command.side_effect = [
            SmolVMError("ioctl(TUNSETIFF): Device or resource busy"),
            MagicMock(stdout=""),
        ]

        nm = NetworkManager()
        nm.create_tap("tap7", "alice")

        assert mock_run_command.call_count == 2
        mock_sleep.assert_called_once_with(0.1)

    @patch("smolvm.network.time.sleep")
    @patch("smolvm.network.run_command")
    def test_create_tap_raises_after_busy_retries(
        self, mock_run_command: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """Persistent busy errors should still fail after retries."""
        mock_run_command.side_effect = SmolVMError("ioctl(TUNSETIFF): Device or resource busy")

        nm = NetworkManager()
        try:
            nm.create_tap("tap7", "alice")
            raise AssertionError("Expected SmolVMError")
        except SmolVMError:
            pass

        assert mock_run_command.call_count == 4
        assert mock_sleep.call_count == 3


class TestLocalPortForwarding:
    """Tests for localhost-only forwarding rule setup/cleanup."""

    @patch("smolvm.network.run_command")
    def test_setup_local_port_forward_adds_output_and_forward(
        self,
        mock_run_command: MagicMock,
    ) -> None:
        """Local forward should add OUTPUT/POSTROUTING/FORWARD, not PREROUTING."""
        mock_run_command.return_value = MagicMock(stdout="")

        nm = NetworkManager()

        nm.setup_local_port_forward(
            vm_id="vm001",
            guest_ip="172.16.0.2",
            host_port=18080,
            guest_port=8080,
        )

        scripts = _collect_nft_scripts(mock_run_command)
        assert "add rule ip smolvm_nat output" in scripts
        assert "add rule ip smolvm_nat postrouting" in scripts
        assert "add rule inet smolvm_filter forward" in scripts
        assert "add rule ip smolvm_nat prerouting" not in scripts

    @patch("smolvm.network.run_command")
    def test_cleanup_local_port_forward_deletes_rules(self, mock_run_command: MagicMock) -> None:
        """Cleanup should batch-delete OUTPUT/POSTROUTING/FORWARD rules."""

        def _side_effect(cmd: list[str], *args: object, **kwargs: object) -> MagicMock:
            if cmd == ["nft", "-a", "list", "table", "ip", "smolvm_nat"]:
                return MagicMock(
                    stdout=(
                        "table ip smolvm_nat {\n"
                        "  chain output {\n"
                        '    tcp dport 18080 comment "smolvm:vm001:local:18080:8080" # handle 23\n'
                        "  }\n"
                        "  chain postrouting {\n"
                        '    tcp dport 8080 comment "smolvm:vm001:local:18080:8080" # handle 21\n'
                        "  }\n"
                        "}\n"
                    )
                )
            if cmd == ["nft", "-a", "list", "table", "inet", "smolvm_filter"]:
                return MagicMock(
                    stdout=(
                        "table inet smolvm_filter {\n"
                        "  chain forward {\n"
                        '    tcp dport 8080 comment "smolvm:vm001:local:18080:8080" # handle 22\n'
                        "  }\n"
                        "}\n"
                    )
                )
            return MagicMock(stdout="")

        mock_run_command.side_effect = _side_effect

        nm = NetworkManager()
        nm.cleanup_local_port_forward(
            vm_id="vm001",
            guest_ip="172.16.0.2",
            host_port=18080,
            guest_port=8080,
        )

        scripts = _collect_nft_scripts(mock_run_command)
        assert "delete rule ip smolvm_nat postrouting handle 21" in scripts
        assert "delete rule ip smolvm_nat output handle 23" in scripts
        assert "delete rule inet smolvm_filter forward handle 22" in scripts

    @patch("smolvm.network.run_command", side_effect=SmolVMError("missing rule"))
    def test_cleanup_local_port_forward_is_idempotent_when_rules_missing(
        self,
        mock_run_command: MagicMock,
    ) -> None:
        """Missing rules should not raise cleanup errors."""
        nm = NetworkManager()
        nm.cleanup_local_port_forward(
            vm_id="vm001",
            guest_ip="172.16.0.2",
            host_port=18080,
            guest_port=8080,
        )
        # One table list attempt per table (nat + filter).
        assert mock_run_command.call_count == 2

    @patch("smolvm.network.run_command")
    def test_cleanup_all_local_port_forwards_removes_matching_rules_only(
        self,
        mock_run_command: MagicMock,
    ) -> None:
        """Bulk cleanup should remove only local-forward rules for the VM."""
        nm = NetworkManager()

        def _side_effect(cmd: list[str], *args: object, **kwargs: object) -> MagicMock:
            if cmd == ["nft", "-a", "list", "table", "ip", "smolvm_nat"]:
                return MagicMock(
                    stdout=(
                        "table ip smolvm_nat {\n"
                        "  chain output {\n"
                        '    tcp dport 18080 comment "smolvm:vm001:local:18080:8080" # handle 31\n'
                        '    tcp dport 18081 comment "smolvm:other:local:18081:8081" # handle 32\n'
                        "  }\n"
                        "  chain postrouting {\n"
                        '    tcp dport 8080 comment "smolvm:vm001:local:18080:8080" # handle 33\n'
                        "  }\n"
                        "}\n"
                    )
                )
            if cmd == ["nft", "-a", "list", "table", "inet", "smolvm_filter"]:
                return MagicMock(
                    stdout=(
                        "table inet smolvm_filter {\n"
                        "  chain forward {\n"
                        '    tcp dport 8080 comment "smolvm:vm001:local:18080:8080" # handle 34\n'
                        '    tcp dport 22 comment "smolvm:vm001:ssh" # handle 35\n'
                        "  }\n"
                        "}\n"
                    )
                )
            return MagicMock(stdout="")

        mock_run_command.side_effect = _side_effect

        nm.cleanup_all_local_port_forwards("vm001")

        commands = [call.args[0] for call in mock_run_command.call_args_list]
        assert ["nft", "-a", "list", "table", "ip", "smolvm_nat"] in commands
        assert ["nft", "-a", "list", "table", "inet", "smolvm_filter"] in commands

        scripts = _collect_nft_scripts(mock_run_command)
        assert "delete rule ip smolvm_nat output handle 31" in scripts
        assert "delete rule ip smolvm_nat postrouting handle 33" in scripts
        assert "delete rule inet smolvm_filter forward handle 34" in scripts

        # Must not delete rule belonging to another VM.
        assert "delete rule ip smolvm_nat output handle 32" not in scripts
        # Must not delete non-local (SSH) rule.
        assert "delete rule inet smolvm_filter forward handle 35" not in scripts


class TestNetworkPrerequisites:
    """Tests for runtime prerequisite checks."""

    @patch("smolvm.network.os.geteuid", return_value=1000)
    @patch("smolvm.network.run_command")
    def test_check_network_prerequisites_checks_scoped_sudo_commands(
        self,
        mock_run_command: MagicMock,
        mock_geteuid: MagicMock,
    ) -> None:
        """Non-root checks should validate command-scoped sudo access."""
        mock_run_command.return_value = MagicMock(stdout="")

        errors = check_network_prerequisites()

        assert errors == []
        commands = [call.args[0] for call in mock_run_command.call_args_list]
        assert ["ip", "link", "show"] in commands
        assert ["nft", "list", "tables"] in commands
        assert ["sysctl", "net.ipv4.ip_forward"] in commands


class TestEgressAllowlist:
    """Tests for egress allowlist rule behavior."""

    def test_apply_egress_allowlist_applies_atomic_add_then_delete_update(
        self,
    ) -> None:
        """Allowlist updates should stage new rules before deleting old ones."""
        nm = NetworkManager()
        nm._outbound_interface = "eth0"
        nm._ensure_nftables_base = MagicMock()
        nm._nft_list_table = MagicMock(
            return_value=(
                "table inet smolvm_filter {\n"
                "  chain forward {\n"
                '    iifname "tap42" ct state established,related counter accept comment "smolvm:egress:tap42:established" # handle 41\n'
                '    iifname "tap42" counter drop comment "smolvm:egress:tap42:drop" # handle 42\n'
                '    iifname "tap42" oifname "eth0" counter accept comment "smolvm:nat:tap:tap42:to:eth0" # handle 43\n'
                "  }\n"
                "}\n"
            )
        )
        nm._run_nft_script = MagicMock()

        nm.apply_egress_allowlist("tap42", ["1.1.1.1", "8.8.8.8"])

        assert nm._nft_list_table.call_count == 2
        # First call is the main atomic script; subsequent calls are element deletes.
        script = nm._run_nft_script.call_args_list[0].args[0]

        add_established = (
            'add rule inet smolvm_filter forward iifname "tap42" ct state established,related '
            'counter accept comment "smolvm:egress:tap42:established"'
        )
        add_allow = (
            'add rule inet smolvm_filter forward iifname "tap42" ip daddr { 1.1.1.1, 8.8.8.8 } '
            'counter accept comment "smolvm:egress:tap42:allow"'
        )
        add_drop = (
            'add rule inet smolvm_filter forward iifname "tap42" ip daddr != { 1.1.1.1, 8.8.8.8 } '
            'counter drop comment "smolvm:egress:tap42:drop"'
        )
        delete_old_established = "delete rule inet smolvm_filter forward handle 41"
        delete_old_drop = "delete rule inet smolvm_filter forward handle 42"
        delete_old_nat_accept = "delete rule inet smolvm_filter forward handle 43"

        assert add_established in script
        assert add_allow in script
        assert add_drop in script
        assert delete_old_established in script
        assert delete_old_drop in script
        assert delete_old_nat_accept in script

        # Fail-closed sequencing: all adds are staged before old rules are deleted.
        assert script.index(add_drop) < script.index(delete_old_established)

        # TAP should also be removed from allowed_taps set.
        all_scripts = "\n".join(c.args[0] for c in nm._run_nft_script.call_args_list)
        assert 'delete element inet smolvm_filter allowed_taps { "tap42" }' in all_scripts
