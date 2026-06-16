# Copyright 2026 Celesto AI
# Licensed under the Apache License, Version 2.0
# ctypes binding for libkrun (used by libkrun runtime adapter).

from __future__ import annotations

import ctypes
import ctypes.util
import platform
from collections.abc import Mapping
from pathlib import Path

from smolvm.exceptions import SmolVMError

# Kernel image format values accepted by krun_set_kernel.
KERNEL_FORMAT_RAW = 0
KERNEL_FORMAT_ELF = 1
KERNEL_FORMAT_PE_GZ = 2
KERNEL_FORMAT_IMAGE_BZ2 = 3
KERNEL_FORMAT_IMAGE_GZ = 4
KERNEL_FORMAT_IMAGE_ZSTD = 5


def _candidate_library_names() -> list[str]:
    system = platform.system()
    if system == "Darwin":
        # Bare names (work if DYLD_LIBRARY_PATH is set or the lib is in /usr/local/lib)
        names = ["libkrun.1.dylib", "libkrun.dylib"]
        # Homebrew on Apple Silicon and Intel — probe absolute paths so we don't
        # require the caller to set DYLD_LIBRARY_PATH.
        for prefix in ("/opt/homebrew", "/usr/local"):
            names.append(f"{prefix}/lib/libkrun.1.dylib")
            names.append(f"{prefix}/lib/libkrun.dylib")
        return names
    # Linux
    return ["libkrun.so.1", "libkrun.so", "/usr/local/lib/libkrun.so.1"]


def _load_library() -> ctypes.CDLL:
    last_err: OSError | None = None
    for name in _candidate_library_names():
        try:
            return ctypes.CDLL(name, use_errno=True)
        except OSError as exc:
            last_err = exc
    found: str | None = ctypes.util.find_library("krun")
    if found is not None:
        try:
            return ctypes.CDLL(found, use_errno=True)
        except OSError as exc:
            last_err = exc
    raise SmolVMError(
        "Could not load libkrun. Install libkrun (>= 1.9) and ensure the "
        "shared library is on your loader path.",
        {"last_error": str(last_err) if last_err else None},
    )


_lib: ctypes.CDLL | None = None


def _libkrun() -> ctypes.CDLL:
    global _lib
    if _lib is None:
        lib = _load_library()

        lib.krun_create_ctx.argtypes = []
        lib.krun_create_ctx.restype = ctypes.c_int32

        lib.krun_free_ctx.argtypes = [ctypes.c_uint32]
        lib.krun_free_ctx.restype = ctypes.c_int32

        lib.krun_set_vm_config.argtypes = [ctypes.c_uint32, ctypes.c_uint8, ctypes.c_uint32]
        lib.krun_set_vm_config.restype = ctypes.c_int32

        lib.krun_set_root_disk.argtypes = [ctypes.c_uint32, ctypes.c_char_p]
        lib.krun_set_root_disk.restype = ctypes.c_int32

        lib.krun_add_disk.argtypes = [
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_bool,
        ]
        lib.krun_add_disk.restype = ctypes.c_int32

        lib.krun_set_kernel.argtypes = [
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_char_p,
        ]
        lib.krun_set_kernel.restype = ctypes.c_int32

        lib.krun_add_vsock_port.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_char_p]
        lib.krun_add_vsock_port.restype = ctypes.c_int32

        lib.krun_set_env.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_char_p)]
        lib.krun_set_env.restype = ctypes.c_int32

        lib.krun_start_enter.argtypes = [ctypes.c_uint32]
        lib.krun_start_enter.restype = ctypes.c_int32

        _lib = lib
    return _lib


def is_available() -> bool:
    try:
        _libkrun()
        return True
    except SmolVMError:
        return False


def _check(rc: int, op: str) -> None:
    if rc < 0:
        raise SmolVMError(f"libkrun {op} failed", {"return_code": rc})


def _encode(value: str | Path | None) -> bytes | None:
    if value is None:
        return None
    return str(value).encode("utf-8")


def _build_envp(env: Mapping[str, str] | None) -> ctypes.Array[ctypes.c_char_p] | None:
    if env is None:
        return None
    items = [f"{key}={value}".encode() for key, value in env.items()]
    arr = (ctypes.c_char_p * (len(items) + 1))()
    for i, item in enumerate(items):
        arr[i] = item
    arr[len(items)] = None
    return arr


class KrunContext:
    """RAII wrapper around a libkrun context id."""

    def __init__(self) -> None:
        lib = _libkrun()
        rc = lib.krun_create_ctx()
        if rc < 0:
            raise SmolVMError("krun_create_ctx failed", {"return_code": rc})
        self._ctx_id = ctypes.c_uint32(rc).value
        self._closed = False

    @property
    def ctx_id(self) -> int:
        return self._ctx_id

    def set_vm_config(self, vcpus: int, memory_mib: int) -> None:
        _check(_libkrun().krun_set_vm_config(self._ctx_id, vcpus, memory_mib), "krun_set_vm_config")

    def set_root_disk(self, disk_path: Path) -> None:
        _check(
            _libkrun().krun_set_root_disk(self._ctx_id, _encode(disk_path)), "krun_set_root_disk"
        )

    def add_disk(self, block_id: str, disk_path: Path, read_only: bool = False) -> None:
        _check(
            _libkrun().krun_add_disk(
                self._ctx_id, _encode(block_id), _encode(disk_path), read_only
            ),
            "krun_add_disk",
        )

    def set_kernel(
        self,
        kernel_path: Path,
        cmdline: str,
        *,
        initramfs: Path | None = None,
        kernel_format: int = KERNEL_FORMAT_ELF,
    ) -> None:
        _check(
            _libkrun().krun_set_kernel(
                self._ctx_id,
                _encode(kernel_path),
                kernel_format,
                _encode(initramfs),
                _encode(cmdline),
            ),
            "krun_set_kernel",
        )

    def add_vsock_port(self, port: int, uds_path: Path) -> None:
        _check(
            _libkrun().krun_add_vsock_port(self._ctx_id, port, _encode(uds_path)),
            "krun_add_vsock_port",
        )

    def set_env(self, env: Mapping[str, str]) -> None:
        envp = _build_envp(env)
        _check(_libkrun().krun_set_env(self._ctx_id, envp), "krun_set_env")

    def start_enter(self) -> int:
        """Block on the guest. Returns the libkrun exit code (>=0)."""
        rc = _libkrun().krun_start_enter(self._ctx_id)
        return rc

    def close(self) -> None:
        if self._closed:
            return
        try:
            _libkrun().krun_free_ctx(self._ctx_id)
        finally:
            self._closed = True

    def __enter__(self) -> KrunContext:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = [
    "KERNEL_FORMAT_RAW",
    "KERNEL_FORMAT_ELF",
    "KERNEL_FORMAT_PE_GZ",
    "KERNEL_FORMAT_IMAGE_BZ2",
    "KERNEL_FORMAT_IMAGE_GZ",
    "KERNEL_FORMAT_IMAGE_ZSTD",
    "KrunContext",
    "is_available",
]
