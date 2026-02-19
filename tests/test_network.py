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
    def test_setup_ssh_port_forward_adds_rules(self, mock_run_command: MagicMock) -> None:
        """Setup should add prerouting/output/postrouting and forward rules."""
        mock_run_command.return_value = MagicMock(stdout="")

        nm = NetworkManager()
        nm._outbound_interface = "eth0"

        nm.setup_ssh_port_forward(vm_id="vm001", guest_ip="172.16.0.2", host_port=2200)

        scripts = _collect_nft_scripts(mock_run_command)
        assert "add rule ip smolvm_nat prerouting" in scripts
        assert "add rule ip smolvm_nat output" in scripts
        assert "add rule ip smolvm_nat postrouting" in scripts
        assert "add rule inet smolvm_filter forward" in scripts
        assert 'comment "smolvm:vm001:ssh"' in scripts

    @patch("smolvm.network.run_command")
    def test_cleanup_ssh_port_forward_deletes_rules(self, mock_run_command: MagicMock) -> None:
        """Cleanup should delete postrouting/forward/output/prerouting rules."""

        def _side_effect(cmd: list[str], *args: object, **kwargs: object) -> MagicMock:
            if cmd == ["nft", "-a", "list", "chain", "ip", "smolvm_nat", "postrouting"]:
                return MagicMock(stdout='tcp dport 22 comment "smolvm:vm001:ssh" # handle 11\n')
            if cmd == ["nft", "-a", "list", "chain", "inet", "smolvm_filter", "forward"]:
                return MagicMock(stdout='tcp dport 22 comment "smolvm:vm001:ssh" # handle 12\n')
            if cmd == ["nft", "-a", "list", "chain", "ip", "smolvm_nat", "output"]:
                return MagicMock(stdout='tcp dport 2200 comment "smolvm:vm001:ssh" # handle 13\n')
            if cmd == ["nft", "-a", "list", "chain", "ip", "smolvm_nat", "prerouting"]:
                return MagicMock(stdout='tcp dport 2200 comment "smolvm:vm001:ssh" # handle 14\n')
            return MagicMock(stdout="")

        mock_run_command.side_effect = _side_effect

        nm = NetworkManager()
        nm.cleanup_ssh_port_forward(vm_id="vm001", guest_ip="172.16.0.2", host_port=2200)

        commands = [call.args[0] for call in mock_run_command.call_args_list]
        assert [
            "nft",
            "delete",
            "rule",
            "ip",
            "smolvm_nat",
            "postrouting",
            "handle",
            "11",
        ] in commands
        assert [
            "nft",
            "delete",
            "rule",
            "inet",
            "smolvm_filter",
            "forward",
            "handle",
            "12",
        ] in commands
        assert [
            "nft",
            "delete",
            "rule",
            "ip",
            "smolvm_nat",
            "output",
            "handle",
            "13",
        ] in commands
        assert [
            "nft",
            "delete",
            "rule",
            "ip",
            "smolvm_nat",
            "prerouting",
            "handle",
            "14",
        ] in commands


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
        """Cleanup should delete OUTPUT/POSTROUTING/FORWARD local-forward rules."""

        def _side_effect(cmd: list[str], *args: object, **kwargs: object) -> MagicMock:
            comment = 'comment "smolvm:vm001:local:18080:8080"'
            if cmd == ["nft", "-a", "list", "chain", "ip", "smolvm_nat", "postrouting"]:
                return MagicMock(stdout=f"{comment} # handle 21\n")
            if cmd == ["nft", "-a", "list", "chain", "inet", "smolvm_filter", "forward"]:
                return MagicMock(stdout=f"{comment} # handle 22\n")
            if cmd == ["nft", "-a", "list", "chain", "ip", "smolvm_nat", "output"]:
                return MagicMock(stdout=f"{comment} # handle 23\n")
            return MagicMock(stdout="")

        mock_run_command.side_effect = _side_effect

        nm = NetworkManager()
        nm.cleanup_local_port_forward(
            vm_id="vm001",
            guest_ip="172.16.0.2",
            host_port=18080,
            guest_port=8080,
        )

        commands = [call.args[0] for call in mock_run_command.call_args_list]
        assert [
            "nft",
            "delete",
            "rule",
            "ip",
            "smolvm_nat",
            "postrouting",
            "handle",
            "21",
        ] in commands
        assert [
            "nft",
            "delete",
            "rule",
            "inet",
            "smolvm_filter",
            "forward",
            "handle",
            "22",
        ] in commands
        assert [
            "nft",
            "delete",
            "rule",
            "ip",
            "smolvm_nat",
            "output",
            "handle",
            "23",
        ] in commands

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
        # One chain list attempt per cleanup target chain.
        assert mock_run_command.call_count == 3

    @patch("smolvm.network.run_command")
    def test_cleanup_all_local_port_forwards_removes_matching_rules_only(
        self,
        mock_run_command: MagicMock,
    ) -> None:
        """Bulk cleanup should remove only local-forward rules for the VM."""
        nm = NetworkManager()

        def _side_effect(cmd: list[str], *args: object, **kwargs: object) -> MagicMock:
            if cmd == ["nft", "-a", "list", "chain", "ip", "smolvm_nat", "output"]:
                return MagicMock(
                    stdout=(
                        'tcp dport 18080 comment "smolvm:vm001:local:18080:8080" # handle 31\n'
                        'tcp dport 18081 comment "smolvm:other:local:18081:8081" # handle 32\n'
                    )
                )
            if cmd == ["nft", "-a", "list", "chain", "ip", "smolvm_nat", "postrouting"]:
                return MagicMock(
                    stdout='tcp dport 8080 comment "smolvm:vm001:local:18080:8080" # handle 33\n'
                )
            if cmd == ["nft", "-a", "list", "chain", "inet", "smolvm_filter", "forward"]:
                return MagicMock(
                    stdout=(
                        'tcp dport 8080 comment "smolvm:vm001:local:18080:8080" # handle 34\n'
                        'tcp dport 22 comment "smolvm:vm001:ssh" # handle 35\n'
                    )
                )
            return MagicMock(stdout="")

        mock_run_command.side_effect = _side_effect

        nm.cleanup_all_local_port_forwards("vm001")

        commands = [call.args[0] for call in mock_run_command.call_args_list]
        assert ["nft", "-a", "list", "chain", "ip", "smolvm_nat", "output"] in commands
        assert ["nft", "-a", "list", "chain", "ip", "smolvm_nat", "postrouting"] in commands
        assert ["nft", "-a", "list", "chain", "inet", "smolvm_filter", "forward"] in commands

        assert [
            "nft",
            "delete",
            "rule",
            "ip",
            "smolvm_nat",
            "output",
            "handle",
            "31",
        ] in commands
        assert [
            "nft",
            "delete",
            "rule",
            "ip",
            "smolvm_nat",
            "postrouting",
            "handle",
            "33",
        ] in commands
        assert [
            "nft",
            "delete",
            "rule",
            "inet",
            "smolvm_filter",
            "forward",
            "handle",
            "34",
        ] in commands

        # Must not delete rule belonging to another VM.
        assert [
            "nft",
            "delete",
            "rule",
            "ip",
            "smolvm_nat",
            "output",
            "handle",
            "32",
        ] not in commands
        # Must not delete non-local (SSH) rule.
        assert [
            "nft",
            "delete",
            "rule",
            "inet",
            "smolvm_filter",
            "forward",
            "handle",
            "35",
        ] not in commands


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
