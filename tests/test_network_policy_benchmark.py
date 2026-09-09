"""The benchmark must not race snapshot reservations or hide workload failures."""

import inspect
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import smolvm


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
