"""Public capability contract, without launching QEMU or preparing images."""

import pytest

from smolvm._network_policy import validate_network_policy_options
from smolvm.exceptions import ValidationError
from smolvm.types import InternetSettings


@pytest.mark.parametrize("host", ["linux", "darwin"])
@pytest.mark.parametrize("mode", ["open", "off"])
def test_portable_slirp_accepts_folders_and_forwarding(monkeypatch, host, mode):
    monkeypatch.setattr("smolvm._network_policy.sys.platform", host)
    validate_network_policy_options(
        InternetSettings(mode=mode),
        backend="qemu",
        guest_os="ubuntu",
        comm_channel="ssh",
        has_mounts=True,
        has_forwards=True,
    )


@pytest.mark.parametrize("mode", ["off", "restricted"])
@pytest.mark.parametrize("channel", [None, "ssh", "vsock"])
def test_linux_tap_accepts_folders_and_control_channels(monkeypatch, mode, channel):
    monkeypatch.setattr("smolvm._network_policy.sys.platform", "linux")
    validate_network_policy_options(
        InternetSettings(mode=mode, allowed_cidrs=["203.0.113.1"] if mode == "restricted" else []),
        backend="qemu",
        guest_os="ubuntu",
        qemu_network="tap",
        comm_channel=channel,
        has_mounts=True,
    )


@pytest.mark.parametrize("host", ["linux", "darwin"])
def test_cidrs_never_silently_switch_slirp_to_tap(monkeypatch, host):
    monkeypatch.setattr("smolvm._network_policy.sys.platform", host)
    with pytest.raises(ValidationError, match="network='tap'.*qemu_network='tap'"):
        validate_network_policy_options(
            InternetSettings(mode="restricted", allowed_cidrs=["203.0.113.1"]),
            backend="qemu",
            guest_os="ubuntu",
        )


def test_macos_tap_restrictions_fail_with_portable_example(monkeypatch):
    monkeypatch.setattr("smolvm._network_policy.sys.platform", "darwin")
    with pytest.raises(ValidationError, match="mode='off'.*network='slirp'"):
        validate_network_policy_options(
            InternetSettings(mode="off"),
            backend="qemu",
            guest_os="ubuntu",
            qemu_network="tap",
        )


def test_tap_launch_time_forwarding_names_supported_alternative(monkeypatch):
    monkeypatch.setattr("smolvm._network_policy.sys.platform", "linux")
    with pytest.raises(ValidationError, match=r"expose_local\(8080\)"):
        validate_network_policy_options(
            InternetSettings(mode="off"),
            backend="qemu",
            guest_os="ubuntu",
            qemu_network="tap",
            has_forwards=True,
        )


@pytest.mark.parametrize("guest", ["windows", "macos"])
def test_other_guests_are_not_silently_supported(guest):
    with pytest.raises(ValidationError, match="guest does not support"):
        validate_network_policy_options(
            InternetSettings(mode="off"), backend="qemu", guest_os=guest
        )


def test_legacy_slirp_domains_remain_unsupported():
    with pytest.raises(ValidationError):
        validate_network_policy_options(
            InternetSettings(allowed_domains=["example.com"]),
            backend="qemu",
            guest_os="ubuntu",
        )
