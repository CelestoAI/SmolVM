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


class TestSSHPortForwarding:
    """Tests for SSH forwarding rule setup/cleanup."""

    @patch("smolvm.network.run_command")
    @patch.object(NetworkManager, "_rule_exists")
    def test_setup_ssh_port_forward_adds_rules(
        self,
        mock_rule_exists: MagicMock,
        mock_run_command: MagicMock,
    ) -> None:
        """Test setup creates PREROUTING, OUTPUT, and FORWARD rules."""
        mock_rule_exists.return_value = False
        nm = NetworkManager()
        nm._outbound_interface = "eth0"

        nm.setup_ssh_port_forward(
            vm_id="vm001",
            guest_ip="172.16.0.2",
            host_port=2200,
        )

        commands = [call.args[0] for call in mock_run_command.call_args_list]
        assert any("-A" in cmd and "PREROUTING" in cmd for cmd in commands)
        assert any("-A" in cmd and "OUTPUT" in cmd for cmd in commands)
        assert any("-A" in cmd and "POSTROUTING" in cmd for cmd in commands)
        assert any("-A" in cmd and "FORWARD" in cmd for cmd in commands)

    @patch("smolvm.network.run_command")
    def test_cleanup_ssh_port_forward_deletes_rules(self, mock_run_command: MagicMock) -> None:
        """Test cleanup removes FORWARD, OUTPUT and PREROUTING rules."""
        nm = NetworkManager()
        nm._outbound_interface = "eth0"

        nm.cleanup_ssh_port_forward(
            vm_id="vm001",
            guest_ip="172.16.0.2",
            host_port=2200,
        )

        commands = [call.args[0] for call in mock_run_command.call_args_list]
        assert any("-D" in cmd and "POSTROUTING" in cmd for cmd in commands)
        assert any("-D" in cmd and "FORWARD" in cmd for cmd in commands)
        assert any("-D" in cmd and "OUTPUT" in cmd for cmd in commands)
        assert any("-D" in cmd and "PREROUTING" in cmd for cmd in commands)


class TestLocalPortForwarding:
    """Tests for localhost-only forwarding rule setup/cleanup."""

    @patch("smolvm.network.run_command")
    @patch.object(NetworkManager, "_rule_exists")
    def test_setup_local_port_forward_adds_output_and_forward(
        self,
        mock_rule_exists: MagicMock,
        mock_run_command: MagicMock,
    ) -> None:
        """Local forward should create OUTPUT and FORWARD, not PREROUTING."""
        mock_rule_exists.return_value = False
        nm = NetworkManager()

        nm.setup_local_port_forward(
            vm_id="vm001",
            guest_ip="172.16.0.2",
            host_port=18080,
            guest_port=8080,
        )

        commands = [call.args[0] for call in mock_run_command.call_args_list]
        assert any("-A" in cmd and "OUTPUT" in cmd for cmd in commands)
        assert any("-A" in cmd and "POSTROUTING" in cmd for cmd in commands)
        assert any("-A" in cmd and "FORWARD" in cmd for cmd in commands)
        assert not any("-A" in cmd and "PREROUTING" in cmd for cmd in commands)

    @patch("smolvm.network.run_command")
    def test_cleanup_local_port_forward_deletes_rules(self, mock_run_command: MagicMock) -> None:
        """Local-forward cleanup should remove OUTPUT and FORWARD rules."""
        nm = NetworkManager()

        nm.cleanup_local_port_forward(
            vm_id="vm001",
            guest_ip="172.16.0.2",
            host_port=18080,
            guest_port=8080,
        )

        commands = [call.args[0] for call in mock_run_command.call_args_list]
        assert any("-D" in cmd and "POSTROUTING" in cmd for cmd in commands)
        assert any("-D" in cmd and "FORWARD" in cmd for cmd in commands)
        assert any("-D" in cmd and "OUTPUT" in cmd for cmd in commands)
        assert not any("-D" in cmd and "PREROUTING" in cmd for cmd in commands)

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
        assert mock_run_command.call_count == 3

    @patch("smolvm.network.run_command")
    def test_cleanup_all_local_port_forwards_removes_matching_rules_only(
        self,
        mock_run_command: MagicMock,
    ) -> None:
        """Bulk cleanup should only delete local-forward rules for the given VM."""
        nm = NetworkManager()

        def _side_effect(cmd: list[str], *args: object, **kwargs: object) -> MagicMock:
            if cmd == ["iptables", "-t", "nat", "-S", "OUTPUT"]:
                return MagicMock(
                    stdout=(
                        '-A OUTPUT -d 127.0.0.1/32 -p tcp --dport 18080 '
                        '-m comment --comment "smolvm:vm001:local:18080:8080" '
                        "-j DNAT --to-destination 172.16.0.2:8080\n"
                        '-A OUTPUT -d 127.0.0.1/32 -p tcp --dport 18081 '
                        '-m comment --comment "smolvm:other:local:18081:8081" '
                        "-j DNAT --to-destination 172.16.0.3:8081\n"
                    )
                )
            if cmd == ["iptables", "-S", "FORWARD"]:
                return MagicMock(
                    stdout=(
                        '-A FORWARD -p tcp -d 172.16.0.2 --dport 8080 '
                        '-m conntrack --ctstate NEW,ESTABLISHED,RELATED '
                        '-m comment --comment "smolvm:vm001:local:18080:8080" -j ACCEPT\n'
                        '-A FORWARD -p tcp -d 172.16.0.2 --dport 22 '
                        '-m comment --comment "smolvm:vm001:ssh" -j ACCEPT\n'
                    )
                )
            return MagicMock(stdout="")

        mock_run_command.side_effect = _side_effect

        nm.cleanup_all_local_port_forwards("vm001")

        commands = [call.args[0] for call in mock_run_command.call_args_list]
        assert ["iptables", "-t", "nat", "-S", "OUTPUT"] in commands
        assert ["iptables", "-S", "FORWARD"] in commands
        assert [
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
            "18080",
            "-m",
            "comment",
            "--comment",
            "smolvm:vm001:local:18080:8080",
            "-j",
            "DNAT",
            "--to-destination",
            "172.16.0.2:8080",
        ] in commands
        assert [
            "iptables",
            "-D",
            "FORWARD",
            "-p",
            "tcp",
            "-d",
            "172.16.0.2",
            "--dport",
            "8080",
            "-m",
            "conntrack",
            "--ctstate",
            "NEW,ESTABLISHED,RELATED",
            "-m",
            "comment",
            "--comment",
            "smolvm:vm001:local:18080:8080",
            "-j",
            "ACCEPT",
        ] in commands


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
        assert ["iptables", "-L"] in commands
        assert ["sysctl", "net.ipv4.ip_forward"] in commands
