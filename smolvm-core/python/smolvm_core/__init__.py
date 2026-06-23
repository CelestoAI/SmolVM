"""SmolVM Core -- native acceleration for SmolVM internals.

Most applications should import :mod:`smolvm`, not this package directly.
``smolvm_core`` is the optional native helper used by SmolVM for faster
Linux networking and QEMU monitor control.

The public Python surface here is intentionally small: capability checks,
Linux network/sysctl helpers, and disk/image helpers. Native QMP support is
private and is exposed to users through :class:`smolvm.qmp.QMPClient`, which
provides the stable error and fallback behavior.
"""

from ._smolvm_core import (
    _firecracker_request,  # noqa: F401
    _firecracker_wait_for_socket,  # noqa: F401
)
from ._smolvm_core import (
    add_addr as _native_add_addr,
)
from ._smolvm_core import (
    add_route as _native_add_route,
)
from ._smolvm_core import (
    clone_or_sparse_copy as _native_clone_or_sparse_copy,
)
from ._smolvm_core import (
    configure_tap as _native_configure_tap,
)
from ._smolvm_core import (
    create_tap as _native_create_tap,
)
from ._smolvm_core import (
    decompress_zstd_sparse as _native_decompress_zstd_sparse,
)
from ._smolvm_core import (
    delete_tap as _native_delete_tap,
)
from ._smolvm_core import (
    flush_addrs as _native_flush_addrs,
)
from ._smolvm_core import (
    get_default_interface as _native_get_default_interface,
)
from ._smolvm_core import (
    has_native_disk_io as _native_has_native_disk_io,
)
from ._smolvm_core import (
    has_native_firecracker_api as _native_has_native_firecracker_api,
)
from ._smolvm_core import (
    has_native_networking as _native_has_native_networking,
)
from ._smolvm_core import (
    has_native_qmp as _native_has_native_qmp,
)
from ._smolvm_core import (
    prepare_tap as _native_prepare_tap,
)
from ._smolvm_core import (
    set_link_up as _native_set_link_up,
)
from ._smolvm_core import (
    write_sysctl as _native_write_sysctl,
)


def has_native_networking() -> bool:
    """Return True when the native Linux networking helpers can be used.

    This is True on Linux builds of ``smolvm-core``. It is False on macOS,
    where SmolVM uses QEMU user-mode networking instead of TAP setup.
    """

    return bool(_native_has_native_networking())


def has_native_qmp() -> bool:
    """Return True when the private native QMP accelerator is present.

    Use :class:`smolvm.qmp.QMPClient` for QMP operations. The native
    ``_QmpClient`` class is intentionally kept under the private extension
    module so SmolVM can preserve one stable public QMP API.
    """

    return bool(_native_has_native_qmp())


def has_native_disk_io() -> bool:
    """Return True when native disk/image helpers are present."""

    return bool(_native_has_native_disk_io())


def has_native_firecracker_api() -> bool:
    """Return True when the private Firecracker API accelerator is present."""

    return bool(_native_has_native_firecracker_api())


def is_available() -> bool:
    """Return True when native Linux networking helpers are available.

    This compatibility alias is kept for existing callers. New code should use
    :func:`has_native_networking` for clarity, especially on macOS where native
    QMP may be available while native networking is not.
    """

    return has_native_networking()


def create_tap(name: str, owner_uid: int) -> None:
    """Create a TAP network device owned by ``owner_uid``.

    Raises:
        OSError: If the platform does not support the native helper or the
            kernel rejects the operation.
    """

    _native_create_tap(name, owner_uid)


def delete_tap(name: str) -> None:
    """Delete a TAP network device.

    Raises:
        OSError: If the platform does not support the native helper or the
            kernel rejects the operation.
    """

    _native_delete_tap(name)


def set_link_up(name: str) -> None:
    """Bring a network link up."""

    _native_set_link_up(name)


def flush_addrs(name: str) -> None:
    """Remove all addresses from a network link."""

    _native_flush_addrs(name)


def add_addr(name: str, ip: str, prefix_len: int) -> None:
    """Add an IPv4 address with prefix length to a network link."""

    _native_add_addr(name, ip, prefix_len)


def configure_tap(name: str, host_ip: str, prefix_len: int) -> None:
    """Assign an IPv4 address to a TAP link and bring it up."""

    _native_configure_tap(name, host_ip, prefix_len)


def prepare_tap(
    name: str,
    owner_uid: int,
    host_ip: str,
    prefix_len: int,
    route_localnet: bool = True,
) -> None:
    """Create and configure a TAP link in one native operation."""

    _native_prepare_tap(name, owner_uid, host_ip, prefix_len, route_localnet)


def add_route(dest: str, prefix_len: int, dev: str) -> None:
    """Add a route for ``dest/prefix_len`` through ``dev``."""

    _native_add_route(dest, prefix_len, dev)


def get_default_interface() -> str:
    """Return the default outbound network interface name."""

    return str(_native_get_default_interface())


def write_sysctl(key: str, value: str) -> None:
    """Write a Linux sysctl key using dot notation.

    Example:
        ``write_sysctl("net.ipv4.ip_forward", "1")``

    Raises:
        OSError: If the platform does not support the native helper or the
            kernel rejects the operation.
    """

    _native_write_sysctl(key, value)


def clone_or_sparse_copy(source: str, target: str) -> str:
    """Copy a disk image using reflink or sparse-preserving native I/O."""

    return str(_native_clone_or_sparse_copy(source, target))


def decompress_zstd_sparse(source: str, target: str, chunk_size: int = 1048576) -> str:
    """Decompress a zstd image while preserving zero regions as sparse holes."""

    return str(_native_decompress_zstd_sparse(source, target, chunk_size))


__all__ = [
    "has_native_networking",
    "has_native_qmp",
    "has_native_disk_io",
    "has_native_firecracker_api",
    "is_available",
    "create_tap",
    "delete_tap",
    "set_link_up",
    "flush_addrs",
    "add_addr",
    "configure_tap",
    "prepare_tap",
    "add_route",
    "get_default_interface",
    "write_sysctl",
    "clone_or_sparse_copy",
    "decompress_zstd_sparse",
]
