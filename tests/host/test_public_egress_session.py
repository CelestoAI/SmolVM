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

"""Deterministic tests for private public-egress lifecycle ownership."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest

from smolvm.host._public_egress_proxy import PublicProxyEndpoint
from smolvm.host._public_egress_session import (
    PublicEgressDiagnostic,
    PublicEgressSession,
    PublicEgressSessionController,
    PublicEgressSessionError,
    PublicEgressSessionState,
)
from smolvm.types import NetworkConfig


@dataclass
class FakeProxy:
    host: str
    port: int
    events: list[str]
    fail_start: bool = False
    fail_stop: bool = False
    alive: bool = False

    @property
    def endpoint(self) -> PublicProxyEndpoint:
        return PublicProxyEndpoint(self.host, self.port)

    @property
    def running(self) -> bool:
        return self.alive

    def start(self) -> PublicProxyEndpoint:
        self.events.append(f"proxy.start:{self.port}")
        if self.fail_start:
            raise OSError("raw bind details must not escape")
        self.alive = True
        return self.endpoint

    def stop(self) -> None:
        self.events.append(f"proxy.stop:{self.port}")
        self.alive = False
        if self.fail_stop:
            raise OSError("raw close details must not escape")


class FakeProxyFactory:
    def __init__(self, events: list[str], *, first_port: int = 41000) -> None:
        self.events = events
        self.next_port = first_port
        self.fail_next_start = False
        self.instances: list[FakeProxy] = []
        self.calls: list[tuple[str, str]] = []

    def __call__(self, session_id: str, listen_host: str) -> FakeProxy:
        self.calls.append((session_id, listen_host))
        proxy = FakeProxy(
            listen_host,
            self.next_port,
            self.events,
            fail_start=self.fail_next_start,
        )
        self.fail_next_start = False
        self.next_port += 1
        self.instances.append(proxy)
        return proxy


class FakeNetworkAdapter:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.fail_apply = False
        self.fail_remove = False
        self.applied: list[tuple[str, str, str, int]] = []
        self.removed: list[str] = []
        self.present_taps: set[str] = set()

    def apply_public_proxy_policy(
        self,
        tap_device: str,
        *,
        guest_ip: str,
        proxy_host_ip: str,
        proxy_port: int,
    ) -> None:
        self.events.append(f"policy.apply:{tap_device}:{proxy_port}")
        if self.fail_apply:
            raise OSError("raw nft details must not escape")
        self.applied.append((tap_device, guest_ip, proxy_host_ip, proxy_port))
        self.present_taps.add(tap_device)

    def remove_network_policy(self, tap_device: str) -> None:
        self.events.append(f"policy.remove:{tap_device}")
        self.removed.append(tap_device)
        if self.fail_remove:
            raise OSError("raw nft cleanup details must not escape")

    def tap_exists(self, tap_device: str) -> bool:
        return tap_device in self.present_taps

    def teardown_tap(self, tap_device: str) -> None:
        self.events.append(f"tap.teardown:{tap_device}")
        self.present_taps.discard(tap_device)


def _network(index: int = 2) -> NetworkConfig:
    return NetworkConfig(
        guest_ip=f"172.16.0.{index}",
        gateway_ip="172.16.0.1",
        netmask="255.255.255.0",
        tap_device=f"tap{index}",
        guest_mac=f"02:00:00:00:00:{index:02x}",
    )


def _session(
    *,
    guest_network: str = "tap",
    factory: FakeProxyFactory | None = None,
    adapter: FakeNetworkAdapter | None = None,
    diagnostics: list[PublicEgressDiagnostic] | None = None,
) -> tuple[PublicEgressSession, FakeProxyFactory, FakeNetworkAdapter, list[str]]:
    events: list[str] = [] if factory is None else factory.events
    factory = factory or FakeProxyFactory(events)
    adapter = adapter or FakeNetworkAdapter(events)
    session = PublicEgressSession(
        "vm-one",
        guest_network=guest_network,  # type: ignore[arg-type]
        network=_network(),
        proxy_factory=factory,
        network_adapter=adapter,
        diagnostic_sink=None if diagnostics is None else diagnostics.append,
    )
    return session, factory, adapter, events


def test_tap_proxy_and_policy_are_ready_before_guest_launch() -> None:
    session, factory, adapter, events = _session()

    def launch(metadata: NetworkConfig) -> str:
        events.append(f"guest.launch:{metadata.egress_proxy_host_port}")
        assert session.state is PublicEgressSessionState.READY
        return "started"

    assert session.launch(launch) == "started"
    assert events == ["proxy.start:41000", "policy.apply:tap2:41000", "guest.launch:41000"]
    assert factory.calls == [("vm-one", "172.16.0.1")]
    assert adapter.applied == [("tap2", "172.16.0.2", "172.16.0.1", 41000)]


def test_qemu_slirp_returns_guestfwd_metadata_without_tap_policy() -> None:
    session, factory, adapter, events = _session(guest_network="qemu-slirp")

    metadata = session.prepare()

    assert metadata.egress_proxy_host_port == 41000
    assert factory.calls == [("vm-one", "127.0.0.1")]
    assert adapter.applied == []
    assert events == ["proxy.start:41000"]


def test_bind_failure_is_redacted_and_never_launches_guest() -> None:
    session, factory, _adapter, _events = _session()
    factory.fail_next_start = True
    launched = False

    def launch(_metadata: NetworkConfig) -> None:
        nonlocal launched
        launched = True

    with pytest.raises(PublicEgressSessionError) as caught:
        session.launch(launch)

    assert launched is False
    assert "raw bind details" not in str(caught.value)
    assert factory.instances[0].alive is False
    assert session.state is PublicEgressSessionState.FAILED_CLOSED


def test_policy_failure_stops_proxy_without_releasing_tap_enforcement() -> None:
    session, factory, adapter, events = _session()
    adapter.fail_apply = True

    with pytest.raises(PublicEgressSessionError, match="could not be installed"):
        session.launch(lambda _metadata: events.append("guest.launch"))

    assert "guest.launch" not in events
    assert factory.instances[0].alive is False
    assert adapter.removed == []
    assert events == [
        "proxy.start:41000",
        "policy.apply:tap2:41000",
        "proxy.stop:41000",
    ]


def test_failed_guest_start_stops_proxy_and_retains_policy() -> None:
    session, factory, adapter, _events = _session()

    def fail(_metadata: NetworkConfig) -> None:
        raise RuntimeError("raw guest failure must not escape")

    with pytest.raises(PublicEgressSessionError) as caught:
        session.launch(fail)

    assert "raw guest failure" not in str(caught.value)
    assert factory.instances[0].alive is False
    assert adapter.removed == []
    assert session.state is PublicEgressSessionState.FAILED_CLOSED


def test_normal_stop_and_delete_are_idempotent() -> None:
    session, factory, adapter, events = _session()
    session.prepare()

    session.stop()
    session.stop()
    session.delete()
    session.delete()

    assert events.count("proxy.stop:41000") == 1
    assert adapter.removed == []
    assert factory.instances[0].alive is False
    assert session.state is PublicEgressSessionState.DELETED

    with pytest.raises(PublicEgressSessionError, match="still exists"):
        session.finalize_guest_teardown()
    adapter.teardown_tap("tap2")
    session.finalize_guest_teardown()
    session.finalize_guest_teardown()
    assert adapter.removed == ["tap2"]
    assert session.cleanup_complete is True


def test_restart_replaces_proxy_and_policy_before_launch() -> None:
    session, factory, adapter, events = _session()
    session.prepare()
    events.clear()

    metadata = session.restart(lambda current: current)

    assert metadata.egress_proxy_host_port == 41001
    assert events == [
        "proxy.stop:41000",
        "proxy.start:41001",
        "policy.apply:tap2:41001",
    ]
    assert len(factory.instances) == 2
    assert adapter.removed == []


def test_dead_proxy_changes_state_but_keeps_fail_closed_tap_policy() -> None:
    diagnostics: list[PublicEgressDiagnostic] = []
    session, factory, adapter, _events = _session(diagnostics=diagnostics)
    session.prepare()
    factory.instances[0].alive = False

    with pytest.raises(PublicEgressSessionError, match="stopped"):
        session.require_ready()

    assert session.state is PublicEgressSessionState.FAILED_CLOSED
    assert adapter.removed == []
    assert diagnostics[-1].reason == "proxy_stopped"
    assert not hasattr(diagnostics[-1], "url")


def test_controller_isolates_simultaneous_sessions() -> None:
    events: list[str] = []
    factory = FakeProxyFactory(events)
    adapter = FakeNetworkAdapter(events)
    controller = PublicEgressSessionController(
        proxy_factory=factory,
        network_adapter=adapter,
        shutdown_registrar=None,
    )
    first = controller.create("vm-a", guest_network="tap", network=_network(2))
    second = controller.create("vm-b", guest_network="tap", network=_network(3))

    first_metadata = first.prepare()
    second_metadata = second.prepare()

    assert first_metadata.egress_proxy_host_port == 41000
    assert second_metadata.egress_proxy_host_port == 41001
    assert factory.calls == [("vm-a", "172.16.0.1"), ("vm-b", "172.16.0.1")]
    assert adapter.applied == [
        ("tap2", "172.16.0.2", "172.16.0.1", 41000),
        ("tap3", "172.16.0.3", "172.16.0.1", 41001),
    ]
    with pytest.raises(PublicEgressSessionError, match="already exists"):
        controller.create("vm-a", guest_network="tap", network=_network(4))


def test_controller_delete_retains_name_until_tap_teardown_is_proven() -> None:
    events: list[str] = []
    factory = FakeProxyFactory(events)
    adapter = FakeNetworkAdapter(events)
    controller = PublicEgressSessionController(
        proxy_factory=factory,
        network_adapter=adapter,
        shutdown_registrar=None,
    )
    controller.create("vm-a", guest_network="tap", network=_network()).prepare()

    controller.delete("vm-a")
    controller.delete("vm-a")
    with pytest.raises(PublicEgressSessionError, match="already exists"):
        controller.create("vm-a", guest_network="qemu-slirp", network=_network())

    adapter.teardown_tap("tap2")
    controller.finalize_guest_teardown("vm-a")
    replacement = controller.create("vm-a", guest_network="qemu-slirp", network=_network())

    assert replacement.state is PublicEgressSessionState.STOPPED
    assert factory.instances[0].alive is False
    assert adapter.removed == ["tap2"]


def test_host_shutdown_stops_proxies_but_keeps_surviving_guests_fail_closed() -> None:
    events: list[str] = []
    factory = FakeProxyFactory(events)
    adapter = FakeNetworkAdapter(events)
    callbacks: list[Callable[[], None]] = []
    controller = PublicEgressSessionController(
        proxy_factory=factory,
        network_adapter=adapter,
        shutdown_registrar=callbacks.append,
    )
    controller.create("vm-a", guest_network="tap", network=_network(2)).prepare()
    controller.create("vm-b", guest_network="tap", network=_network(3)).prepare()

    assert len(callbacks) == 1
    callbacks[0]()
    callbacks[0]()

    assert all(not proxy.alive for proxy in factory.instances)
    assert adapter.removed == []
    assert controller.get("vm-a").state is PublicEgressSessionState.DELETED
    assert controller.get("vm-b").state is PublicEgressSessionState.DELETED
    with pytest.raises(PublicEgressSessionError, match="controller is stopped"):
        controller.create("vm-c", guest_network="tap", network=_network(4))


def test_diagnostics_are_typed_redacted_and_sink_failures_do_not_break_start() -> None:
    diagnostics: list[PublicEgressDiagnostic] = []
    session, _factory, _adapter, _events = _session(diagnostics=diagnostics)
    session.prepare()

    assert [(event.state, event.reason) for event in diagnostics] == [
        (PublicEgressSessionState.STARTING, None),
        (PublicEgressSessionState.READY, None),
    ]
    assert all(
        set(vars(event)) == {"session_id", "state", "guest_network", "reason"}
        for event in diagnostics
    )

    broken_sink_session, factory, adapter, _ = _session()
    broken_sink_session = PublicEgressSession(
        "vm-sink",
        guest_network="tap",
        network=_network(),
        proxy_factory=factory,
        network_adapter=adapter,
        diagnostic_sink=lambda _event: (_ for _ in ()).throw(RuntimeError("sink failed")),
    )
    assert broken_sink_session.prepare().egress_proxy_host_port == 41000


def test_finalize_failure_retains_policy_and_reports_redacted_error() -> None:
    session, factory, adapter, _events = _session()
    session.prepare()
    session.stop()
    adapter.teardown_tap("tap2")
    adapter.fail_remove = True

    with pytest.raises(PublicEgressSessionError) as caught:
        session.finalize_guest_teardown()

    assert "raw nft cleanup" not in str(caught.value)
    assert factory.instances[0].alive is False
    assert session.state is PublicEgressSessionState.STOPPED

    adapter.fail_remove = False
    session.finalize_guest_teardown()
    assert adapter.removed == ["tap2", "tap2"]
    assert session.cleanup_complete is True


def test_controller_retains_failed_finalize_for_retry() -> None:
    events: list[str] = []
    factory = FakeProxyFactory(events)
    adapter = FakeNetworkAdapter(events)
    controller = PublicEgressSessionController(
        proxy_factory=factory,
        network_adapter=adapter,
        shutdown_registrar=None,
    )
    controller.create("vm-a", guest_network="tap", network=_network()).prepare()
    controller.delete("vm-a")
    adapter.teardown_tap("tap2")
    adapter.fail_remove = True

    with pytest.raises(PublicEgressSessionError, match="cleanup"):
        controller.finalize_guest_teardown("vm-a")
    assert controller.get("vm-a").state is PublicEgressSessionState.DELETED

    adapter.fail_remove = False
    controller.finalize_guest_teardown("vm-a")
    with pytest.raises(PublicEgressSessionError, match="not found"):
        controller.get("vm-a")
