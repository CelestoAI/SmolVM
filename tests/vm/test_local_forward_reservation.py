"""Local port ownership without QEMU, SSH, or firewall privileges."""

import socket
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from smolvm.facade import SmolVM
from smolvm.types import VMState


@pytest.fixture
def sandboxes():
    instances = []

    def create(name):
        vm = object.__new__(SmolVM)
        vm._vm_id = name
        vm._info = SimpleNamespace(
            status=VMState.RUNNING,
            config=SimpleNamespace(backend="qemu", qemu_network="tap", internet_settings=None),
            network=SimpleNamespace(mode="nat", guest_ip="172.16.0.2"),
        )
        vm._sdk = MagicMock()
        vm._local_forwards = {}
        vm._refresh_info = lambda: None
        vm._probe_local_forward = MagicMock(return_value=True)
        vm._start_local_tunnel = MagicMock(side_effect=AssertionError("Unexpected SSH fallback"))
        instances.append(vm)
        return vm

    yield create
    for vm in instances:
        vm._cleanup_local_forwards()


def assert_available(port):
    with socket.socket() as check:
        check.bind(("127.0.0.1", port))


def test_two_sandboxes_cannot_claim_the_same_port(sandboxes):
    first, second = sandboxes("first"), sandboxes("second")
    port = first.expose_local(8080)
    assert first.expose_local(8080, host_port=port) == port
    first._sdk.network.setup_local_port_forward.assert_called_once()
    other = second.expose_local(8080, host_port=port)
    assert other != port
    assert second._sdk.network.setup_local_port_forward.call_args.kwargs["host_port"] == other
    # A third process/listener cannot bind either held port. The reservation
    # does not listen, so it cannot itself make the application probe succeed.
    with socket.socket() as check, pytest.raises(OSError):
        check.bind(("127.0.0.1", port))
    assert not SmolVM._probe_local_forward(port, timeout=0.05)
    first.unexpose_local(port, 8080)
    assert_available(port)
    assert first.expose_local(8080, host_port=port) == port


def test_existing_listener_is_not_redirected(sandboxes):
    vm = sandboxes("listener-collision")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        occupied = listener.getsockname()[1]
        assert vm.expose_local(8080, host_port=occupied) != occupied
        assert vm._sdk.network.setup_local_port_forward.call_args.kwargs["host_port"] != occupied


@pytest.mark.parametrize("failure", ["install", "probe"])
def test_failed_exposure_releases_reservations(sandboxes, failure):
    vm = sandboxes("failed")
    if failure == "install":
        vm._sdk.network.setup_local_port_forward.side_effect = RuntimeError("nft failed")
    else:
        vm._probe_local_forward.return_value = False
    with pytest.raises(Exception, match="Failed to expose"):
        vm.expose_local(8080)
    assert not vm._local_forwards
    for call in vm._sdk.network.setup_local_port_forward.call_args_list:
        assert_available(call.kwargs["host_port"])


def test_cleanup_failure_still_closes_socket(sandboxes):
    vm = sandboxes("cleanup-failure")
    port = vm.expose_local(8080)
    vm._sdk.network.cleanup_local_port_forward.side_effect = RuntimeError("nft failed")
    with pytest.raises(RuntimeError, match="nft failed"):
        vm.unexpose_local(port, 8080)
    assert_available(port)
