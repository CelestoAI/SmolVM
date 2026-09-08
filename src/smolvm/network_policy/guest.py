"""Configure supported guest clients; the host firewall remains the boundary."""

import ssl
import tempfile
from pathlib import Path
from typing import Protocol

from smolvm.types import CommandResult, VMInfo

from .firewall import NetworkBinding

_CERT_PATH = "/usr/local/share/ca-certificates/smolvm-network-policy.crt"
_BUNDLE_PATH = "/etc/ssl/certs/ca-certificates.crt"


class GuestChannel(Protocol):
    def put_file(self, local_path: str | Path, remote_path: str) -> None: ...
    def run(self, command: str, *, timeout: float) -> CommandResult: ...
    def set_managed_env(self, variables: dict[str, str]) -> dict[str, str]: ...


def configure_guest(channel: GuestChannel, binding: NetworkBinding, public_ca: bytes) -> None:
    """Install public trust and managed environment before exposing exec/terminal.

    Requires the standard Linux guest image's ca-certificates package. Existing
    long-lived application TLS contexts are not retroactively reconfigured.
    The lifecycle caller must stop the VM if this operation fails.
    """
    if binding.proxy_port is None:
        raise ValueError("Deny-all sandboxes do not have proxy trust to install.")
    if b"PRIVATE KEY" in public_ca or public_ca.count(b"-----BEGIN CERTIFICATE-----") != 1:
        raise ValueError("Only one public CA certificate may be copied into the sandbox.")
    # Parse the certificate without importing the optional proxy engine.
    ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=public_ca.decode("ascii"))
    with tempfile.TemporaryDirectory(prefix="smolvm-public-ca-") as directory:
        certificate = Path(directory) / "smolvm-network-policy.crt"
        certificate.write_bytes(public_ca)
        certificate.chmod(0o644)
        channel.put_file(certificate, _CERT_PATH)
    result = channel.run("update-ca-certificates --fresh", timeout=30)
    if result.exit_code != 0:
        raise RuntimeError("Could not configure HTTPS trust; use a current SmolVM Linux image.")
    proxy = f"http://{binding.gateway_ip}:{binding.proxy_port}"
    # Bypassing the gateway here preserves platform services, not unrestricted
    # host access: the host firewall allows only explicit platform ports.
    bypass = f"localhost,127.0.0.1,::1,{binding.gateway_ip}"
    environment = {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "NO_PROXY": bypass,
        "no_proxy": bypass,
        "SSL_CERT_FILE": _BUNDLE_PATH,
        "REQUESTS_CA_BUNDLE": _BUNDLE_PATH,
        "CURL_CA_BUNDLE": _BUNDLE_PATH,
        "GIT_SSL_CAINFO": _BUNDLE_PATH,
        "NODE_EXTRA_CA_CERTS": _CERT_PATH,
    }
    applied = channel.set_managed_env(environment)
    if any(applied.get(name) != value for name, value in environment.items()):
        raise RuntimeError("Could not configure the sandbox's proxy environment.")


def configure_started_guest(vm: VMInfo, vm_pid: int, *, timeout: float) -> None:
    """Complete native boot before returning a usable restricted runtime."""
    from smolvm.comm.rust_http_vsock_channel import RustHttpVsockChannel

    from .placement import active_policy

    binding, state = active_policy(vm, vm_pid)
    if not vm.config.network_policy.allowed_domains:
        return
    if vm.config.vsock is None:
        raise RuntimeError("Strict networking requires the sandbox control channel.")
    channel = RustHttpVsockChannel.from_cid(vm.config.vsock.guest_cid, sandbox_name=vm.vm_id)
    try:
        channel.wait_ready(timeout=timeout)
        configure_guest(channel, binding, state["public_ca"].encode("ascii"))
        # Readiness or certificate installation is not evidence that the host
        # worker remained healthy while the guest was booting.
        active_policy(vm, vm_pid)
    finally:
        channel.close()
