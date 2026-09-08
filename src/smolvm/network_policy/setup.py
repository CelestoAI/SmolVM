"""Strict-mode prerequisites; never install packages during VM creation."""

import importlib.metadata
import shutil
import sys

from . import NetworkPolicy

ENGINE_VERSION = "12.2.3"
_INSTALL = "uv tool install --python 3.12 'smolvm[network-policy]'"


def require_runtime(policy: NetworkPolicy) -> None:
    """Fail before boot rather than silently falling back to unrestricted egress."""
    if sys.platform != "linux":
        raise RuntimeError("Strict network policies require Linux; use a Linux sandbox host.")
    for command in ("nft", "ip"):
        if shutil.which(command) is None:
            raise RuntimeError(
                "Network policy tools are missing; run 'smolvm setup' on this Linux host."
            )
    if not policy.allowed_domains:
        return  # Deny-all has no proxy, Python floor or proxy dependency.
    if sys.version_info < (3, 12):
        raise RuntimeError(f'Strict HTTPS requires Python 3.12 or newer; run "{_INSTALL}".')
    try:
        version = importlib.metadata.version("mitmproxy")
    except importlib.metadata.PackageNotFoundError:
        version = None
    if version != ENGINE_VERSION:
        raise RuntimeError(f'Strict HTTPS support is missing or incompatible; run "{_INSTALL}".')
    if shutil.which("setpriv") is None:
        raise RuntimeError(
            "Process isolation tools are missing; install util-linux on this Linux host."
        )
