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

"""Private lifecycle coordinator for host-owned public egress.

This module deliberately is not connected to :class:`InternetSettings`.  It
coordinates the already-private proxy and backend adapters so their ordering
and cleanup contract can be tested before a public network mode exists.
"""

from __future__ import annotations

import atexit
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Protocol, TypeAlias, TypeVar

from smolvm.exceptions import NetworkError
from smolvm.host._public_egress_proxy import PublicEgressProxy, PublicProxyEndpoint
from smolvm.host.network import NetworkManager
from smolvm.types import NetworkConfig

PublicEgressGuestNetwork = Literal["qemu-slirp", "tap"]
PublicEgressFailureReason = Literal[
    "invalid_network",
    "proxy_start_failed",
    "policy_install_failed",
    "guest_start_failed",
    "proxy_stopped",
    "cleanup_failed",
]

_ResultT = TypeVar("_ResultT")


class PublicEgressSessionState(str, Enum):
    """Internal lifecycle state; no user-selectable policy is implied."""

    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    FAILED_CLOSED = "failed_closed"
    DELETED = "deleted"


@dataclass(frozen=True)
class PublicEgressDiagnostic:
    """Redacted lifecycle diagnostic that never contains request targets."""

    session_id: str
    state: PublicEgressSessionState
    guest_network: PublicEgressGuestNetwork
    reason: PublicEgressFailureReason | None = None


class EgressProxy(Protocol):
    """Proxy operations required by the session coordinator."""

    @property
    def endpoint(self) -> PublicProxyEndpoint: ...

    @property
    def running(self) -> bool: ...

    def start(self) -> PublicProxyEndpoint: ...

    def stop(self) -> None: ...


class PublicProxyNetworkAdapter(Protocol):
    """TAP policy operations required by the session coordinator."""

    def apply_public_proxy_policy(
        self,
        tap_device: str,
        *,
        guest_ip: str,
        proxy_host_ip: str,
        proxy_port: int,
    ) -> None: ...

    def remove_network_policy(self, tap_device: str) -> None: ...

    def tap_exists(self, tap_device: str) -> bool: ...


ProxyFactory: TypeAlias = Callable[[str, str], EgressProxy]
DiagnosticSink: TypeAlias = Callable[[PublicEgressDiagnostic], None]
ShutdownRegistrar: TypeAlias = Callable[[Callable[[], None]], Any]


class PublicEgressSessionError(NetworkError):
    """A private egress lifecycle step failed without opening networking."""


def _default_proxy_factory(session_id: str, listen_host: str) -> EgressProxy:
    return PublicEgressProxy(session_id, listen_host=listen_host)


class PublicEgressSession:
    """Own one fail-closed proxy and its backend policy for one guest."""

    def __init__(
        self,
        session_id: str,
        *,
        guest_network: PublicEgressGuestNetwork,
        network: NetworkConfig,
        proxy_factory: ProxyFactory = _default_proxy_factory,
        network_adapter: PublicProxyNetworkAdapter | None = None,
        diagnostic_sink: DiagnosticSink | None = None,
    ) -> None:
        if not session_id:
            raise ValueError("session_id cannot be empty")
        if guest_network not in {"qemu-slirp", "tap"}:
            raise ValueError("guest_network must be 'qemu-slirp' or 'tap'")
        self.session_id = session_id
        self.guest_network = guest_network
        self._base_network = network
        self._proxy_factory = proxy_factory
        self._network_adapter = network_adapter if network_adapter is not None else NetworkManager()
        self._diagnostic_sink = diagnostic_sink
        self._state = PublicEgressSessionState.STOPPED
        self._proxy: EgressProxy | None = None
        self._active_network: NetworkConfig | None = None
        self._policy_installed = False
        self._lock = threading.RLock()

    @property
    def state(self) -> PublicEgressSessionState:
        with self._lock:
            return self._state

    @property
    def network_metadata(self) -> NetworkConfig:
        """Return metadata only while the proxy is alive and usable."""
        return self.require_ready()

    @property
    def cleanup_complete(self) -> bool:
        """Return whether no retained TAP enforcement remains owned by this session."""
        with self._lock:
            return not self._policy_installed

    def prepare(self) -> NetworkConfig:
        """Start the proxy and install TAP policy before guest execution."""
        with self._lock:
            if self._state is PublicEgressSessionState.DELETED:
                raise PublicEgressSessionError(
                    f"Public-egress session '{self.session_id}' was deleted; recreate it."
                )
            if self._state is PublicEgressSessionState.READY:
                return self.require_ready()
            if self._state is PublicEgressSessionState.STARTING:
                raise PublicEgressSessionError(
                    f"Public-egress session '{self.session_id}' is already starting; retry shortly."
                )
            if self._state is PublicEgressSessionState.FAILED_CLOSED:
                self._cleanup(mark_deleted=False)

            try:
                listen_host = self._listen_host()
            except (TypeError, ValueError) as exc:
                self._fail(PublicEgressSessionState.FAILED_CLOSED, "invalid_network")
                raise PublicEgressSessionError(
                    f"Public-egress network for '{self.session_id}' is incomplete; recreate it."
                ) from exc

            self._state = PublicEgressSessionState.STARTING
            self._emit()
            proxy: EgressProxy | None = None
            try:
                proxy = self._proxy_factory(self.session_id, listen_host)
                self._proxy = proxy
                endpoint = proxy.start()
                self._validate_endpoint(endpoint, listen_host)
            except Exception as exc:
                if proxy is not None:
                    try:
                        proxy.stop()
                    except Exception:
                        self._fail(PublicEgressSessionState.FAILED_CLOSED, "cleanup_failed")
                    else:
                        self._proxy = None
                if not self._policy_installed:
                    self._active_network = None
                if self._state is not PublicEgressSessionState.FAILED_CLOSED:
                    self._fail(PublicEgressSessionState.FAILED_CLOSED, "proxy_start_failed")
                raise PublicEgressSessionError(
                    f"Public-egress proxy for '{self.session_id}' could not start; "
                    "stop it and retry."
                ) from exc

            active_network = NetworkConfig.model_validate(
                {
                    **self._base_network.model_dump(),
                    "egress_proxy_host_port": endpoint.port,
                }
            )
            self._active_network = active_network

            if self.guest_network == "tap":
                assert active_network.guest_ip is not None
                assert active_network.gateway_ip is not None
                # Treat an attempted transaction as owned until its idempotent
                # removal succeeds. This preserves retry data if cleanup fails.
                self._policy_installed = True
                try:
                    self._network_adapter.apply_public_proxy_policy(
                        active_network.tap_device,
                        guest_ip=active_network.guest_ip,
                        proxy_host_ip=active_network.gateway_ip,
                        proxy_port=endpoint.port,
                    )
                except Exception as exc:
                    with suppress(PublicEgressSessionError):
                        self._cleanup(mark_deleted=False, reason="policy_install_failed")
                    raise PublicEgressSessionError(
                        f"Public-egress policy for '{self.session_id}' could not be installed; "
                        "stop it and retry."
                    ) from exc

            self._state = PublicEgressSessionState.READY
            self._emit()
            return active_network

    def launch(self, launch_guest: Callable[[NetworkConfig], _ResultT]) -> _ResultT:
        """Prepare egress, then invoke the guest launch callback exactly once."""
        metadata = self.prepare()
        try:
            return launch_guest(metadata)
        except Exception as exc:
            self._cleanup(mark_deleted=False, reason="guest_start_failed")
            raise PublicEgressSessionError(
                f"Guest '{self.session_id}' could not start with public egress; stop it and retry."
            ) from exc

    def restart(self, launch_guest: Callable[[NetworkConfig], _ResultT]) -> _ResultT:
        """Replace the proxy and policy before starting the guest again."""
        self.stop()
        return self.launch(launch_guest)

    def require_ready(self) -> NetworkConfig:
        """Fail closed when the owned proxy is absent or has stopped."""
        with self._lock:
            proxy = self._proxy
            if (
                self._state is not PublicEgressSessionState.READY
                or proxy is None
                or self._active_network is None
            ):
                raise PublicEgressSessionError(
                    f"Public-egress session '{self.session_id}' is not ready; stop it and retry."
                )
            if not proxy.running:
                self._state = PublicEgressSessionState.FAILED_CLOSED
                self._emit("proxy_stopped")
                raise PublicEgressSessionError(
                    f"Public-egress proxy for '{self.session_id}' stopped; stop it and retry."
                )
            return self._active_network

    def stop(self) -> None:
        """Stop the proxy while retaining TAP enforcement until TAP teardown."""
        self._cleanup(mark_deleted=False)

    def delete(self) -> None:
        """Stop the proxy and retain TAP enforcement until teardown is proven."""
        self._cleanup(mark_deleted=True)

    def finalize_guest_teardown(self) -> None:
        """Release retained TAP policy only after the interface no longer exists."""
        with self._lock:
            if self.guest_network != "tap" or not self._policy_installed:
                self._active_network = None
                return
            active_network = self._active_network
            if active_network is None:
                self._emit("cleanup_failed")
                raise PublicEgressSessionError(
                    f"Public-egress cleanup for '{self.session_id}' is missing network state; "
                    "retry guest teardown."
                )
            if self._network_adapter.tap_exists(active_network.tap_device):
                raise PublicEgressSessionError(
                    f"Guest network for '{self.session_id}' still exists; stop the guest before "
                    "finalizing public-egress cleanup."
                )
            try:
                self._network_adapter.remove_network_policy(active_network.tap_device)
            except Exception as exc:
                self._emit("cleanup_failed")
                raise PublicEgressSessionError(
                    f"Public-egress cleanup for '{self.session_id}' was incomplete; retry cleanup."
                ) from exc
            self._policy_installed = False
            self._active_network = None

    def _cleanup(
        self,
        *,
        mark_deleted: bool,
        reason: PublicEgressFailureReason | None = None,
    ) -> None:
        with self._lock:
            if self._state is PublicEgressSessionState.DELETED:
                return
            proxy = self._proxy
            cleanup_failed = False
            if proxy is not None:
                try:
                    proxy.stop()
                except Exception:
                    cleanup_failed = True
                else:
                    self._proxy = None
            if cleanup_failed:
                self._fail(PublicEgressSessionState.FAILED_CLOSED, "cleanup_failed")
                raise PublicEgressSessionError(
                    f"Public-egress cleanup for '{self.session_id}' was incomplete; retry stop."
                )

            self._proxy = None
            if self.guest_network != "tap" or not self._policy_installed:
                self._active_network = None
            self._state = (
                PublicEgressSessionState.DELETED
                if mark_deleted
                else (
                    PublicEgressSessionState.FAILED_CLOSED
                    if reason is not None
                    else PublicEgressSessionState.STOPPED
                )
            )
            self._emit(reason)

    def _listen_host(self) -> str:
        network = self._base_network
        if network.mode != "nat":
            raise ValueError("public egress requires managed NAT networking")
        if self.guest_network == "qemu-slirp":
            return "127.0.0.1"
        if network.guest_ip is None or network.gateway_ip is None:
            raise ValueError("TAP public egress requires guest and gateway addresses")
        if not network.tap_device.startswith("tap"):
            raise ValueError("TAP public egress requires a managed TAP interface")
        return network.gateway_ip

    @staticmethod
    def _validate_endpoint(endpoint: PublicProxyEndpoint, listen_host: str) -> None:
        if endpoint.host != listen_host or not 1 <= endpoint.port <= 65535:
            raise ValueError("proxy returned an unexpected listener")

    def _fail(
        self,
        state: PublicEgressSessionState,
        reason: PublicEgressFailureReason,
    ) -> None:
        self._state = state
        self._emit(reason)

    def _emit(self, reason: PublicEgressFailureReason | None = None) -> None:
        if self._diagnostic_sink is None:
            return
        event = PublicEgressDiagnostic(
            session_id=self.session_id,
            state=self._state,
            guest_network=self.guest_network,
            reason=reason,
        )
        with suppress(Exception):
            self._diagnostic_sink(event)


class PublicEgressSessionController:
    """Own and stop private egress sessions for one host process."""

    def __init__(
        self,
        *,
        proxy_factory: ProxyFactory = _default_proxy_factory,
        network_adapter: PublicProxyNetworkAdapter | None = None,
        diagnostic_sink: DiagnosticSink | None = None,
        shutdown_registrar: ShutdownRegistrar | None = atexit.register,
    ) -> None:
        self._proxy_factory = proxy_factory
        self._network_adapter = network_adapter if network_adapter is not None else NetworkManager()
        self._diagnostic_sink = diagnostic_sink
        self._sessions: dict[str, PublicEgressSession] = {}
        self._closed = False
        self._lock = threading.RLock()
        if shutdown_registrar is not None:
            shutdown_registrar(self.shutdown)

    def create(
        self,
        session_id: str,
        *,
        guest_network: PublicEgressGuestNetwork,
        network: NetworkConfig,
    ) -> PublicEgressSession:
        """Create one owner for a session ID; duplicate ownership is rejected."""
        with self._lock:
            if self._closed:
                raise PublicEgressSessionError(
                    "Public-egress controller is stopped; create a new controller."
                )
            if session_id in self._sessions:
                raise PublicEgressSessionError(
                    f"Public-egress session '{session_id}' already exists; reuse or delete it."
                )
            session = PublicEgressSession(
                session_id,
                guest_network=guest_network,
                network=network,
                proxy_factory=self._proxy_factory,
                network_adapter=self._network_adapter,
                diagnostic_sink=self._diagnostic_sink,
            )
            self._sessions[session_id] = session
            return session

    def get(self, session_id: str) -> PublicEgressSession:
        with self._lock:
            try:
                return self._sessions[session_id]
            except KeyError as exc:
                raise PublicEgressSessionError(
                    f"Public-egress session '{session_id}' was not found; recreate it."
                ) from exc

    def stop(self, session_id: str) -> None:
        self.get(session_id).stop()

    def delete(self, session_id: str) -> None:
        with self._lock:
            try:
                session = self._sessions[session_id]
            except KeyError:
                return
        session.delete()
        with self._lock:
            if self._sessions.get(session_id) is session and session.cleanup_complete:
                self._sessions.pop(session_id)

    def finalize_guest_teardown(self, session_id: str) -> None:
        """Release retained TAP enforcement after the VM removed its interface."""
        session = self.get(session_id)
        session.finalize_guest_teardown()
        with self._lock:
            if (
                self._sessions.get(session_id) is session
                and session.state is PublicEgressSessionState.DELETED
                and session.cleanup_complete
            ):
                self._sessions.pop(session_id)

    def shutdown(self) -> None:
        """Best-effort host-shutdown cleanup for every owned session."""
        with self._lock:
            if self._closed and not self._sessions:
                return
            self._closed = True
            sessions = tuple(self._sessions.items())
        for session_id, session in sessions:
            try:
                session.delete()
            except Exception:
                continue
            with self._lock:
                if self._sessions.get(session_id) is session and session.cleanup_complete:
                    self._sessions.pop(session_id)
