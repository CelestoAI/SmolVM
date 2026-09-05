"""Tests for the libkrun child-process launcher orchestration."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from smolvm.runtime import _libkrun_launcher as launcher


def test_main_requires_a_config_path(capsys: pytest.CaptureFixture[str]) -> None:
    assert launcher.main(["launcher"]) == 2
    assert "usage:" in capsys.readouterr().err


def test_main_configures_context_and_returns_start_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "vcpus": 2,
                "memory_mib": 512,
                "kernel_path": "/kernel",
                "initrd_path": "/initrd",
                "rootfs_path": "/rootfs",
                "extra_disks": [{"block_id": "data", "path": "/data", "read_only": True}],
                "vsock_ports": [{"port": 1024, "uds_path": "/tmp/agent.sock"}],
                "env": {"MODE": "test"},
            }
        )
    )

    calls: list[tuple[str, object]] = []

    class FakeContext:
        ctx_id = 41

        def __enter__(self) -> "FakeContext":
            calls.append(("enter", None))
            return self

        def __exit__(self, *_args: object) -> None:
            calls.append(("exit", None))

        def set_vm_config(self, vcpus: int, memory: int) -> None:
            calls.append(("vm_config", (vcpus, memory)))

        def set_kernel(self, path: Path, cmdline: str, **kwargs: object) -> None:
            calls.append(("kernel", (path, cmdline, kwargs)))

        def set_root_disk(self, path: Path) -> None:
            calls.append(("root", path))

        def add_disk(self, block_id: str, path: Path, read_only: bool) -> None:
            calls.append(("disk", (block_id, path, read_only)))

        def add_vsock_port(self, port: int, path: Path) -> None:
            calls.append(("vsock", (port, path)))

        def set_env(self, values: dict[str, str]) -> None:
            calls.append(("env", values))

        def start_enter(self) -> int:
            calls.append(("start", None))
            return 0

    monkeypatch.setattr(launcher, "KrunContext", FakeContext)
    monkeypatch.setattr(launcher.sys, "platform", "linux")

    assert launcher.main(["launcher", str(config_path)]) == 0
    assert ("vm_config", (2, 512)) in calls
    assert ("root", Path("/rootfs")) in calls
    assert ("disk", ("data", Path("/data"), True)) in calls
    assert ("vsock", (1024, Path("/tmp/agent.sock"))) in calls
    assert ("env", {"MODE": "test"}) in calls
    assert calls[-2:] == [("start", None), ("exit", None)]


def test_main_returns_one_when_libkrun_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"vcpus": 1, "memory_mib": 128, "kernel_path": "/kernel"}))

    class FakeContext:
        ctx_id = 1

        def __enter__(self) -> "FakeContext":
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def set_vm_config(self, *_args: object) -> None:
            pass

        def set_kernel(self, *_args: object, **_kwargs: object) -> None:
            pass

        def start_enter(self) -> int:
            return -1

    monkeypatch.setattr(launcher, "KrunContext", FakeContext)
    monkeypatch.setattr(launcher.sys, "platform", "linux")

    assert launcher.main(["launcher", str(config_path)]) == 1
