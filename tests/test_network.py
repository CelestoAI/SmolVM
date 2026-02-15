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
        assert any("-D" in cmd and "FORWARD" in cmd for cmd in commands)
        assert any("-D" in cmd and "OUTPUT" in cmd for cmd in commands)
        assert not any("-D" in cmd and "PREROUTING" in cmd for cmd in commands)


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
