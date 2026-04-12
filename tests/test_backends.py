from unittest.mock import patch

from smolvm.backends import BACKEND_LIBKRUN, BACKEND_QEMU, resolve_backend


def test_resolve_backend_auto_prefers_libkrun_on_macos_when_available() -> None:
    with patch("smolvm.backends.platform.system", return_value="Darwin"), patch(
        "smolvm.backends.which", return_value="/usr/local/bin/krunvm"
    ):
        assert resolve_backend("auto") == BACKEND_LIBKRUN


def test_resolve_backend_auto_falls_back_to_qemu_on_macos_without_krunvm() -> None:
    with patch("smolvm.backends.platform.system", return_value="Darwin"), patch(
        "smolvm.backends.which", return_value=None
    ):
        assert resolve_backend("auto") == BACKEND_QEMU


def test_resolve_backend_accepts_libkrun_explicitly() -> None:
    assert resolve_backend(BACKEND_LIBKRUN) == BACKEND_LIBKRUN
