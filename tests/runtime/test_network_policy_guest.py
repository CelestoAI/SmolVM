"""Public-trust handoff and environment setup; no guest-security attestation."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from smolvm.network_policy.firewall import NetworkBinding
from smolvm.network_policy.guest import configure_guest
from smolvm.types import CommandResult


@pytest.fixture
def public_ca():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "test-only")])
    now = datetime.now(UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
    )


class Channel:
    def __init__(self, exit_code=0, mismatch=False):
        self.exit_code = exit_code
        self.mismatch = mismatch
        self.calls = []

    def put_file(self, local_path, remote_path):
        self.calls.append(("certificate", remote_path, Path(local_path).read_bytes()))

    def run(self, command, *, timeout):
        self.calls.append(("command", command, timeout))
        return CommandResult(exit_code=self.exit_code, stdout="", stderr="")

    def set_managed_env(self, variables):
        self.calls.append(("environment", variables))
        return {} if self.mismatch else variables


def binding():
    return NetworkBinding(
        tap="tap1",
        guest_ip="172.16.0.2",
        gateway_ip="172.16.0.1",
        host_addresses=("172.16.0.1",),
        proxy_uid=100001,
        proxy_port=18080,
    )


def test_public_trust_precedes_environment(public_ca):
    channel = Channel()
    configure_guest(channel, binding(), public_ca)
    assert [call[0] for call in channel.calls] == ["certificate", "command", "environment"]
    assert channel.calls[0][2] == public_ca
    assert b"PRIVATE KEY" not in channel.calls[0][2]
    environment = channel.calls[-1][1]
    assert environment["HTTPS_PROXY"] == "http://172.16.0.1:18080"
    assert environment["SSL_CERT_FILE"] == environment["REQUESTS_CA_BUNDLE"]
    assert "172.16.0.1" in environment["NO_PROXY"]


def test_private_key_never_reaches_guest(public_ca):
    channel = Channel()
    with pytest.raises(ValueError, match="public CA"):
        configure_guest(channel, binding(), public_ca + b"-----BEGIN PRIVATE KEY-----")
    assert channel.calls == []


def test_trust_update_failure_is_not_reported_as_configured(public_ca):
    channel = Channel(exit_code=1)
    with pytest.raises(RuntimeError, match="HTTPS trust"):
        configure_guest(channel, binding(), public_ca)
    assert [call[0] for call in channel.calls] == ["certificate", "command"]


def test_partial_environment_is_not_reported_as_configured(public_ca):
    with pytest.raises(RuntimeError, match="proxy environment"):
        configure_guest(Channel(mismatch=True), binding(), public_ca)


@pytest.mark.parametrize("failure", [None, "ready", "configure", "recheck"])
def test_native_guest_setup_closes_channel(monkeypatch, failure):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from smolvm.comm.rust_http_vsock_channel import RustHttpVsockChannel
    from smolvm.network_policy import guest, placement

    vm = SimpleNamespace(
        vm_id="sbx-policy",
        config=SimpleNamespace(
            network_policy=SimpleNamespace(allowed_domains=("example.com",)),
            vsock=SimpleNamespace(guest_cid=42),
        ),
    )
    binding = Mock()
    check = Mock(return_value=(binding, {"public_ca": "certificate"}))
    monkeypatch.setattr(placement, "active_policy", check)
    channel = Mock()
    factory = Mock(return_value=channel)
    monkeypatch.setattr(RustHttpVsockChannel, "from_cid", factory)
    configure = Mock()
    monkeypatch.setattr(guest, "configure_guest", configure)
    if failure == "ready":
        channel.wait_ready.side_effect = RuntimeError("ready")
    elif failure == "configure":
        configure.side_effect = RuntimeError("configure")
    elif failure == "recheck":
        check.side_effect = [(binding, {"public_ca": "certificate"}), RuntimeError("recheck")]
    if failure:
        with pytest.raises(RuntimeError, match=failure):
            guest.configure_started_guest(vm, 1234, timeout=7)
    else:
        guest.configure_started_guest(vm, 1234, timeout=7)
        configure.assert_called_once_with(channel, binding, b"certificate")
        assert check.call_count == 2
    channel.close.assert_called_once()
    factory.assert_called_once_with(42, sandbox_name="sbx-policy")


def test_native_deny_all_needs_no_guest_channel(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from smolvm.comm.rust_http_vsock_channel import RustHttpVsockChannel
    from smolvm.network_policy import guest, placement

    vm = SimpleNamespace(config=SimpleNamespace(network_policy=SimpleNamespace(allowed_domains=())))
    monkeypatch.setattr(placement, "active_policy", Mock(return_value=(Mock(), {})))
    factory = Mock(side_effect=AssertionError("No proxy trust for deny-all"))
    monkeypatch.setattr(RustHttpVsockChannel, "from_cid", factory)
    guest.configure_started_guest(vm, 1234, timeout=7)
    factory.assert_not_called()
