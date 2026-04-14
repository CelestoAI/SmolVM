from unittest.mock import patch

from smolvm.runtime.backends import BACKEND_LIBKRUN, BACKEND_QEMU, resolve_backend


def test_resolve_backend_auto_defaults_to_qemu_on_macos() -> None:
    with patch("smolvm.runtime.backends.platform.system", return_value="Darwin"):
        assert resolve_backend("auto") == BACKEND_QEMU


def test_resolve_backend_auto_defaults_to_qemu_on_macos_even_with_krunvm() -> None:
    with patch("smolvm.runtime.backends.platform.system", return_value="Darwin"), patch(
        "smolvm.runtime.backends.which", return_value="/usr/local/bin/krunvm"
    ):
        assert resolve_backend("auto") == BACKEND_QEMU


def test_resolve_backend_accepts_libkrun_explicitly() -> None:
    assert resolve_backend(BACKEND_LIBKRUN) == BACKEND_LIBKRUN
