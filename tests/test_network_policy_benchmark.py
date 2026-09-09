"""The benchmark must not race snapshot reservations or hide workload failures."""

import inspect
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import smolvm


def test_forward_claim_survives_probe_socket_close(monkeypatch):
    from threading import Lock

    script = Path(__file__).resolve().parents[1] / "scripts/benchmark-network-policy.py"
    module = runpy.run_path(str(script))
    ports = iter([18080, 18080, 18081])

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def bind(self, _address):
            self.port = next(ports)

        def getsockname(self):
            return "127.0.0.1", self.port

    claim = module["claim_forward_port"]
    monkeypatch.setitem(claim.__globals__, "socket", SimpleNamespace(socket=Probe))
    claimed, lock = set(), Lock()
    assert claim(claimed, lock) == 18080
    assert claim(claimed, lock) == 18081
    assert claimed == {18080, 18081}


def test_bind_failure_can_be_identified_from_qemu_log(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/benchmark-network-policy.py"
    check = runpy.run_path(str(script))["is_forward_bind_failure"]
    log = tmp_path / "qemu.log"
    error = RuntimeError("QEMU exited early while booting")
    log.write_text("qemu: Could not set up host forwarding rule 'tcp:127.0.0.1:18080-:18080'")
    assert check(error, log)
    log.write_text("qemu: Could not open disk")
    assert not check(error, log)


@pytest.mark.parametrize("bind_failure", [True, False])
def test_slirp_benchmark_retries_only_bind_conflicts(monkeypatch, tmp_path, bind_failure):
    script = Path(__file__).resolve().parents[1] / "scripts/benchmark-network-policy.py"
    module = runpy.run_path(str(script))
    globals_ = module["main"].__globals__
    ports = iter(range(18080, 18100))
    created, deleted = [], []
    monkeypatch.setattr(smolvm.facade, "generate_sandbox_name", smolvm.facade.generate_sandbox_name)

    class Sandbox:
        def __init__(self, **kwargs):
            self.config = kwargs["config"]
            self._sdk = None
            self.info = SimpleNamespace(network=SimpleNamespace(guest_ip="10.0.2.15"))
            created.append(self)

        def start(self):
            if len(created) == 2:  # Fail the measured sample, not warmup.
                raise RuntimeError("Local port already in use" if bind_failure else "disk failed")

        def stop(self, **kwargs):
            pass

        def delete(self):
            assert self not in deleted
            deleted.append(self)

    monkeypatch.setitem(globals_, "SmolVM", Sandbox)
    monkeypatch.setitem(globals_, "VMConfig", SimpleNamespace(model_validate=lambda config: config))
    monkeypatch.setitem(globals_, "claim_forward_port", lambda *_: next(ports))
    monkeypatch.setitem(globals_, "wait_for_application", lambda *_: None)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"backend": "qemu", "qemu_network": "slirp"}))
    output = tmp_path / "results.jsonl"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(script),
            "--vm-config",
            str(config),
            "--samples",
            "1",
            "--output",
            str(output),
            "--data-dir",
            str(tmp_path),
        ],
    )
    if bind_failure:
        module["main"]()
        rows = [json.loads(line) for line in output.read_text().splitlines()]
        assert rows[1]["forward_bind_retries"] == 1
        assert len(created) == 3
        assert created[1].config["port_forwards"] != created[2].config["port_forwards"]
    else:
        with pytest.raises(RuntimeError, match="disk failed"):
            module["main"]()
        assert len(created) == 2
    assert deleted == created


def test_documented_image_recipe_generates_valid_config(monkeypatch, tmp_path):
    import re

    from smolvm.images import BootImage
    from smolvm.types import VMConfig

    root = Path(__file__).resolve().parents[1]
    document = (root / "docs/deep-dive/qemu-network-policy-validation.md").read_text()
    recipe = re.search(r"uv run python - <<'PY'\n(.*?)\nPY", document, re.DOTALL).group(1)
    kernel, disk = tmp_path / "kernel", tmp_path / "rootfs.ext4"
    kernel.touch()
    disk.touch()

    class Builder:
        def __init__(self, **kwargs):
            assert all(path.is_file() for path in kwargs["context"].values())
            assert "COPY policy-init /policy-init" in kwargs["dockerfile"]

        def build_boot_image(self, **kwargs):
            return BootImage(
                name="fixture",
                rootfs_path=disk,
                kernel_path=kernel,
                rootfs_format="raw-ext4",
                backend="qemu",
                arch="arm64",
                boot=kwargs["boot"],
            )

    monkeypatch.setattr(smolvm, "DockerRootfsBuilder", Builder)
    monkeypatch.setenv("POLICY_BENCH_DIR", str(tmp_path))
    monkeypatch.chdir(root)
    exec(compile(recipe, "documented-image-recipe", "exec"), {})
    config = VMConfig.model_validate_json((tmp_path / "vm-config.json").read_text())
    assert "init=/policy-init" in config.boot_args
    assert config.rootfs_path == disk
    assert config.backend == "qemu"


@pytest.mark.parametrize("fail_restore", [False, True])
def test_benchmark_batches_restore_and_preserves_errors(monkeypatch, tmp_path, fail_restore):
    signature = inspect.signature(smolvm.SmolVM)
    restore_signature = inspect.signature(smolvm.SmolVM.from_snapshot)
    events = []
    snapshots = {}
    # Keep the benchmark's temporary naming override local to this test.
    monkeypatch.setattr(smolvm.facade, "generate_sandbox_name", smolvm.facade.generate_sandbox_name)

    class Sandbox:
        def __init__(self, **kwargs):
            signature.bind(**kwargs)
            self.name = smolvm.facade.generate_sandbox_name(set())
            self.inventory = kwargs["state_manager"]
            self.deleted = False
            self._sdk = SimpleNamespace(delete_snapshot=lambda key: snapshots.pop(key))
            events.append(("create", self.name))

        def start(self):
            pass

        def run(self, _command):
            return SimpleNamespace(exit_code=0)

        def stop(self, timeout=3):
            assert not self.deleted, "Stopped an already-deleted sample"

        def delete(self):
            assert not self.deleted, "Deleted a sample twice"
            self.deleted = True
            events.append(("delete", self.name))

        def snapshot(self, **_kwargs):
            snapshots[self.name] = self
            events.append(("snapshot", self.name))
            return SimpleNamespace(snapshot_id=self.name)

        @classmethod
        def from_snapshot(cls, key, **kwargs):
            restore_signature.bind(key, **kwargs)
            original = snapshots[key]
            assert original.inventory is kwargs["state_manager"]
            assert original.deleted
            if fail_restore:
                raise RuntimeError("original restore failure")
            restored = object.__new__(cls)
            restored.name, restored.inventory = key, original.inventory
            restored.deleted, restored._sdk = False, original._sdk
            events.append(("restore", key))
            return restored

    monkeypatch.setattr(smolvm, "SmolVM", Sandbox)
    output = tmp_path / "timings.jsonl"
    script = Path(__file__).resolve().parents[1] / "scripts/benchmark-network-policy.py"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(script),
            "--samples",
            "4",
            "--concurrency",
            "2",
            "--restore",
            "--url",
            "http://198.18.1.2",
            "--data-dir",
            str(tmp_path),
            "--output",
            str(output),
        ],
    )
    if fail_restore:
        with pytest.raises(RuntimeError, match="original restore failure"):
            runpy.run_path(str(script), run_name="__main__")
    else:
        runpy.run_path(str(script), run_name="__main__")
        rows = [json.loads(line) for line in output.read_text().splitlines()][1:]
        assert [row["sample"] for row in rows] == list(range(4))
        assert all("restore_first_command_ms" in row for row in rows)
        # One warmup, then two batches of two: all original VMs in a batch
        # must be deleted before any restore can reserve their original IPs.
        creates = [name for event, name in events if event == "create"]
        for batch in (creates[:1], creates[1:3], creates[3:5]):
            deletions = [events.index(("delete", name)) for name in batch]
            restores = [events.index(("restore", name)) for name in batch]
            assert max(deletions) < min(restores)
    assert not snapshots
