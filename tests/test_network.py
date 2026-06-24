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

import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from smolvm.exceptions import SmolVMError
from smolvm.host.network import NetworkManager, check_network_prerequisites


@pytest.fixture(autouse=True)
def _disable_native_extension():
    """Force subprocess path in all network tests (native extension may be present on Linux CI)."""
    with patch("smolvm.host.network.HAS_NETLINK", False):
        yield


@pytest.fixture(autouse=True)
def _reset_native_unprivileged_flag():
    """Reset the cached EPERM flag so tests don't leak state into each other."""
    import smolvm.host.network as net

    net._native_unprivileged = False
    yield
    net._native_unprivileged = False


def _collect_nft_scripts(mock_run_command: MagicMock) -> str:
    scripts: list[str] = []
    for call in mock_run_command.call_args_list:
        cmd = call.args[0]
        if cmd == ["nft", "-f", "-"]:
            scripts.append(call.kwargs.get("input", ""))
    return "\n".join(scripts)


class TestSSHPortForwarding:
    """Tests for SSH forwarding rule setup/cleanup."""

    @patch("smolvm.host.network.run_command")
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

    @patch("smolvm.host.network.run_command")
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

    @patch("smolvm.host.network.run_command")
    def test_create_tap_is_idempotent_when_existing(self, mock_run_command: MagicMock) -> None:
        """'File exists' errors should be treated as success."""
        mock_run_command.side_effect = SmolVMError("RTNETLINK answers: File exists")

        nm = NetworkManager()
        nm.create_tap("tap42", "alice")

        mock_run_command.assert_called_once_with(
            ["ip", "tuntap", "add", "tap42", "mode", "tap", "user", "alice"]
        )

    @patch("smolvm.host.network.time.sleep")
    @patch("smolvm.host.network.run_command")
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

    @patch("smolvm.host.network.time.sleep")
    @patch("smolvm.host.network.run_command")
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

    @patch("smolvm.host.network.run_command")
    @patch("smolvm.host.network.network_native")
    def test_native_can_be_forced_off_with_env_var(
        self,
        mock_network_native: MagicMock,
        mock_run_command: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SMOLVM_DISABLE_NATIVE_NETWORKING should leave subprocess commands unchanged."""
        mock_run_command.return_value = MagicMock(stdout="")
        monkeypatch.setenv("SMOLVM_DISABLE_NATIVE_NETWORKING", "yes")

        with patch("smolvm.host.network.HAS_NETLINK", True):
            nm = NetworkManager()
            nm.create_tap("tap2", "alice")

        mock_network_native.create_tap.assert_not_called()
        mock_run_command.assert_called_once_with(
            ["ip", "tuntap", "add", "tap2", "mode", "tap", "user", "alice"]
        )

    @patch("smolvm.host.network.run_command")
    @patch("smolvm.host.network.network_native")
    def test_create_tap_falls_back_to_subprocess_on_eperm(
        self,
        mock_network_native: MagicMock,
        mock_run_command: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When the native ioctl returns EPERM, fall through to sudo ip path."""
        mock_network_native.create_tap.side_effect = OSError("tap2: errno 1")
        mock_run_command.return_value = MagicMock(stdout="")

        caplog.set_level(logging.WARNING, logger="smolvm.host.network")
        with patch("smolvm.host.network.HAS_NETLINK", True):
            nm = NetworkManager()
            nm.create_tap("tap2", "alice")

        mock_network_native.create_tap.assert_called_once()
        mock_run_command.assert_called_once_with(
            ["ip", "tuntap", "add", "tap2", "mode", "tap", "user", "alice"]
        )
        warning = caplog.text
        assert "Fast Rust networking needs permission" in warning
        assert "root or CAP_NET_ADMIN" in warning
        assert "smolvm setup" in warning
        assert "same SmolVM command with sudo" in warning

    @patch("smolvm.host.network.run_command")
    @patch("smolvm.host.network.network_native")
    def test_native_unprivileged_flag_skips_subsequent_native_attempts(
        self, mock_network_native: MagicMock, mock_run_command: MagicMock
    ) -> None:
        """After one EPERM, later calls must not even try the native path."""
        mock_network_native.create_tap.side_effect = OSError("tap2: errno 1")
        mock_run_command.return_value = MagicMock(stdout="")

        with patch("smolvm.host.network.HAS_NETLINK", True):
            nm = NetworkManager()
            nm.create_tap("tap2", "alice")  # trips EPERM, sets flag
            nm.create_tap("tap3", "alice")  # must skip native entirely
            nm.add_route("172.16.0.5", "tap3")  # also must skip native

        # network_native.create_tap was tried exactly once; the second create_tap and the
        # add_route both short-circuited before touching the native module.
        assert mock_network_native.create_tap.call_count == 1
        mock_network_native.add_route.assert_not_called()

    @patch("smolvm.host.network.run_command")
    @patch("smolvm.host.network.Path.write_text", side_effect=PermissionError)
    @patch("smolvm.host.network.network_native")
    def test_native_sysctl_eperm_disables_subsequent_native_attempts(
        self,
        mock_network_native: MagicMock,
        mock_write_text: MagicMock,
        mock_run_command: MagicMock,
    ) -> None:
        """A native EPERM from sysctl should latch fallback for later helpers."""
        mock_network_native.write_sysctl.side_effect = OSError("Operation not permitted")
        mock_run_command.return_value = MagicMock(stdout="")

        with patch("smolvm.host.network.HAS_NETLINK", True):
            nm = NetworkManager()
            nm.enable_ip_forwarding()
            nm.add_route("172.16.0.5", "tap3")

        mock_network_native.write_sysctl.assert_called_once()
        mock_network_native.add_route.assert_not_called()
        mock_write_text.assert_called_once_with("1")
        commands = [call.args[0] for call in mock_run_command.call_args_list]
        assert ["sysctl", "-w", "net.ipv4.ip_forward=1"] in commands
        assert ["ip", "route", "add", "172.16.0.5/32", "dev", "tap3"] in commands


class TestNativeTapManagement:
    """Tests for native TAP/configure/route parity."""

    @patch("smolvm.host.network.run_command")
    @patch("smolvm.host.network.network_native")
    def test_prepare_tap_uses_native_composite_helper(
        self, mock_network_native: MagicMock, mock_run_command: MagicMock
    ) -> None:
        """Sync prepare should leave route_localnet to the Python sysctl path."""
        with patch("smolvm.host.network.HAS_NETLINK", True):
            nm = NetworkManager()
            nm.prepare_tap("tap9", "__missing_user__", host_ip="172.16.0.1", netmask="32")

        mock_network_native.prepare_tap.assert_called_once_with(
            "tap9", os.getuid(), "172.16.0.1", 32, False
        )
        mock_network_native.create_tap.assert_not_called()
        mock_network_native.configure_tap.assert_not_called()
        mock_network_native.write_sysctl.assert_called_once_with(
            "net.ipv4.conf.tap9.route_localnet",
            "1",
        )
        mock_run_command.assert_not_called()

    @patch("smolvm.host.network.run_command", side_effect=SmolVMError("sysctl denied"))
    @patch("smolvm.host.network.Path.write_text", side_effect=PermissionError)
    @patch("smolvm.host.network.network_native")
    def test_prepare_tap_route_localnet_uses_soft_sysctl_path(
        self,
        mock_network_native: MagicMock,
        mock_write_text: MagicMock,
        mock_run_command: MagicMock,
    ) -> None:
        """Native prepare sysctl failures should use the existing soft-failure path."""
        mock_network_native.write_sysctl.side_effect = OSError("sysctl denied")

        with patch("smolvm.host.network.HAS_NETLINK", True):
            nm = NetworkManager()
            nm.prepare_tap("tap9", "__missing_user__", host_ip="172.16.0.1", netmask="32")

        mock_network_native.prepare_tap.assert_called_once_with(
            "tap9", os.getuid(), "172.16.0.1", 32, False
        )
        mock_network_native.write_sysctl.assert_called_once_with(
            "net.ipv4.conf.tap9.route_localnet",
            "1",
        )
        mock_write_text.assert_called_once_with("1")
        mock_run_command.assert_called_once_with(
            ["sysctl", "-w", "net.ipv4.conf.tap9.route_localnet=1"],
            use_sudo=True,
        )

    @patch("smolvm.host.network.network_native")
    def test_prepare_tap_falls_back_when_native_composite_missing(
        self, mock_network_native: MagicMock
    ) -> None:
        """Older smolvm-core wheels should fall back to the existing Python sequence."""
        del mock_network_native.prepare_tap

        with patch("smolvm.host.network.HAS_NETLINK", True):
            nm = NetworkManager()
            nm.create_tap = MagicMock()
            nm.configure_tap = MagicMock()
            nm.prepare_tap("tap9", "alice", host_ip="172.16.0.1", netmask="32")

        nm.create_tap.assert_called_once_with("tap9", "alice")
        nm.configure_tap.assert_called_once_with("tap9", host_ip="172.16.0.1", netmask="32")

    @patch("smolvm.host.network.run_command")
    @patch("smolvm.host.network.network_native")
    def test_configure_tap_uses_composite_native_helper(
        self, mock_network_native: MagicMock, mock_run_command: MagicMock
    ) -> None:
        """Sync configure should use one native helper plus the route_localnet sysctl."""
        with patch("smolvm.host.network.HAS_NETLINK", True):
            nm = NetworkManager()
            nm.configure_tap("tap9", host_ip="172.16.0.1", netmask="32")

        mock_network_native.configure_tap.assert_called_once_with("tap9", "172.16.0.1", 32)
        mock_network_native.flush_addrs.assert_not_called()
        mock_network_native.add_addr.assert_not_called()
        mock_network_native.set_link_up.assert_not_called()
        mock_network_native.write_sysctl.assert_called_once_with(
            "net.ipv4.conf.tap9.route_localnet",
            "1",
        )
        mock_run_command.assert_not_called()

    @patch("smolvm.host.network.run_command")
    @patch("smolvm.host.network.network_native")
    def test_default_interface_uses_native_when_available(
        self, mock_network_native: MagicMock, mock_run_command: MagicMock
    ) -> None:
        """Default interface detection should honor the same native gate."""
        mock_network_native.get_default_interface.return_value = "eth0"

        with patch("smolvm.host.network.HAS_NETLINK", True):
            nm = NetworkManager()
            assert nm.outbound_interface == "eth0"

        mock_network_native.get_default_interface.assert_called_once_with()
        mock_run_command.assert_not_called()

    @pytest.mark.asyncio
    @patch("smolvm.host.network.async_run_command", new_callable=AsyncMock)
    @patch("smolvm.host.network.network_native")
    async def test_async_prepare_tap_uses_native_composite_helper(
        self,
        mock_network_native: MagicMock,
        mock_async_run_command: AsyncMock,
    ) -> None:
        """Async prepare should leave route_localnet to the Python sysctl path."""
        with patch("smolvm.host.network.HAS_NETLINK", True):
            nm = NetworkManager()
            await nm.async_prepare_tap(
                "tap9", "__missing_user__", host_ip="172.16.0.1", netmask="32"
            )

        mock_network_native.prepare_tap.assert_called_once_with(
            "tap9", os.getuid(), "172.16.0.1", 32, False
        )
        mock_network_native.create_tap.assert_not_called()
        mock_network_native.configure_tap.assert_not_called()
        mock_network_native.write_sysctl.assert_called_once_with(
            "net.ipv4.conf.tap9.route_localnet",
            "1",
        )
        mock_async_run_command.assert_not_called()

    @pytest.mark.asyncio
    @patch("smolvm.host.network.async_run_command", new_callable=AsyncMock)
    @patch("smolvm.host.network.network_native")
    async def test_async_tap_route_and_delete_use_native_helpers(
        self,
        mock_network_native: MagicMock,
        mock_async_run_command: AsyncMock,
    ) -> None:
        """Async TAP setup should use native helpers through to_thread when available."""
        with patch("smolvm.host.network.HAS_NETLINK", True):
            nm = NetworkManager()
            await nm.async_create_tap("tap9", "__missing_user__")
            await nm.async_configure_tap("tap9", host_ip="172.16.0.1", netmask="32")
            await nm.async_add_route("172.16.0.5", "tap9")
            await nm.async_cleanup_tap("tap9")

        mock_network_native.create_tap.assert_called_once()
        mock_network_native.configure_tap.assert_called_once_with("tap9", "172.16.0.1", 32)
        mock_network_native.add_route.assert_called_once_with("172.16.0.5", 32, "tap9")
        mock_network_native.delete_tap.assert_called_once_with("tap9")
        mock_network_native.write_sysctl.assert_called_once_with(
            "net.ipv4.conf.tap9.route_localnet",
            "1",
        )
        mock_async_run_command.assert_not_called()

    @pytest.mark.asyncio
    @patch("smolvm.host.network.async_run_command", new_callable=AsyncMock)
    @patch("smolvm.host.network.network_native")
    async def test_async_native_eperm_falls_back_to_subprocess(
        self,
        mock_network_native: MagicMock,
        mock_async_run_command: AsyncMock,
    ) -> None:
        """Async native EPERM should use the existing subprocess fallback command."""
        mock_network_native.add_route.side_effect = OSError("Operation not permitted")
        mock_async_run_command.return_value = MagicMock(stdout="")

        with patch("smolvm.host.network.HAS_NETLINK", True):
            nm = NetworkManager()
            await nm.async_add_route("172.16.0.5", "tap9")

        mock_network_native.add_route.assert_called_once_with("172.16.0.5", 32, "tap9")
        mock_async_run_command.assert_awaited_once_with(
            ["ip", "route", "add", "172.16.0.5/32", "dev", "tap9"]
        )


class TestEpermDetector:
    """Pin the regex so 'errno 13' (EACCES) and friends do not match EPERM."""

    def test_matches_bare_errno_1(self) -> None:
        from smolvm.host.network import _is_eperm

        assert _is_eperm("tap2: errno 1")

    def test_matches_operation_not_permitted(self) -> None:
        from smolvm.host.network import _is_eperm

        assert _is_eperm("Operation not permitted")

    def test_does_not_match_errno_13(self) -> None:
        from smolvm.host.network import _is_eperm

        assert not _is_eperm("tap2: errno 13")

    def test_does_not_match_errno_100(self) -> None:
        from smolvm.host.network import _is_eperm

        assert not _is_eperm("tap2: errno 100")


class TestLocalPortForwarding:
    """Tests for localhost-only forwarding rule setup/cleanup."""

    @patch("smolvm.host.network.run_command")
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

    @patch("smolvm.host.network.run_command")
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

    @patch("smolvm.host.network.run_command", side_effect=SmolVMError("missing rule"))
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

    @patch("smolvm.host.network.run_command")
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

    @patch("smolvm.host.network.os.geteuid", return_value=1000)
    @patch("smolvm.host.network.run_command")
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
        assert ["which", "ip"] in commands
        assert ["which", "nft"] in commands
        assert ["which", "sysctl"] in commands
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
                '    iifname "tap42" ct state established,related counter accept comment "smolvm:egress:tap42:established" # handle 41\n'  # noqa: E501
                '    iifname "tap42" counter drop comment "smolvm:egress:tap42:drop" # handle 42\n'
                '    iifname "tap42" oifname "eth0" counter accept comment "smolvm:nat:tap:tap42:to:eth0" # handle 43\n'  # noqa: E501
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
