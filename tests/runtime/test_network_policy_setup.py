"""Optional support must fail before boot, never weaken network policy."""

import importlib.metadata

import pytest

from smolvm.network_policy import NetworkPolicy, setup


@pytest.fixture
def available(monkeypatch):
    monkeypatch.setattr(setup.sys, "platform", "linux")
    monkeypatch.setattr(setup.sys, "version_info", (3, 12))
    monkeypatch.setattr(setup.shutil, "which", lambda command: "/usr/bin/" + command)
    monkeypatch.setattr(setup.importlib.metadata, "version", lambda name: setup.ENGINE_VERSION)


def test_supported_environment(available):
    setup.require_runtime(NetworkPolicy(allowed_domains=["example.com"]))


@pytest.mark.parametrize("version", [(3, 11), (3, 10)])
def test_old_python_never_silently_disables_proxy(available, monkeypatch, version):
    monkeypatch.setattr(setup.sys, "version_info", version)
    with pytest.raises(RuntimeError, match="Python 3.12"):
        setup.require_runtime(NetworkPolicy(allowed_domains=["example.com"]))


def test_deny_all_does_not_need_engine_or_new_python(available, monkeypatch):
    monkeypatch.setattr(setup.sys, "version_info", (3, 11))

    def must_not_query(name):
        pytest.fail("deny-all must not load/query the optional engine")

    monkeypatch.setattr(setup.importlib.metadata, "version", must_not_query)
    setup.require_runtime(NetworkPolicy(allowed_domains=[]))


@pytest.mark.parametrize("version", [None, "12.2.2", "13.0.0"])
def test_missing_or_unreviewed_engine_is_rejected(available, monkeypatch, version):
    def installed(name):
        if version is None:
            raise importlib.metadata.PackageNotFoundError(name)
        return version

    monkeypatch.setattr(setup.importlib.metadata, "version", installed)
    with pytest.raises(RuntimeError, match=r"smolvm\[network-policy\]"):
        setup.require_runtime(NetworkPolicy(allowed_domains=["example.com"]))


@pytest.mark.parametrize("command", ["ip", "nft", "setpriv"])
def test_missing_os_dependency_is_rejected(available, monkeypatch, command):
    monkeypatch.setattr(setup.shutil, "which", lambda name: None if name == command else name)
    with pytest.raises(RuntimeError, match="missing"):
        setup.require_runtime(NetworkPolicy(allowed_domains=["example.com"]))


def test_non_linux_deny_all_is_not_a_false_promise(available, monkeypatch):
    monkeypatch.setattr(setup.sys, "platform", "darwin")
    with pytest.raises(RuntimeError, match="Linux"):
        setup.require_runtime(NetworkPolicy(allowed_domains=[]))
