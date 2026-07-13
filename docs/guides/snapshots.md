# Save and restore a sandbox

A snapshot saves a supported sandbox so you can bring it back later. Create the sandbox without workspace mounts or extra drives when you know you will need a snapshot.

## Create a snapshot

Create a named snapshot from a sandbox:

```bash
smolvm sandbox snapshot create demo --snapshot-id demo-before-change
```

List saved snapshots:

```bash
smolvm sandbox snapshot list --vm-id demo
```

Restore the snapshot when you need it:

```bash
smolvm sandbox snapshot restore demo-before-change
```

## Choose a snapshot type

`full` is the default and saves disk plus running-state information. `diff` stores only changes from the base image, so it needs that base image to remain available. `disk` saves a self-contained disk without RAM, so restore starts the guest from disk rather than resuming it.

```bash
smolvm sandbox snapshot create demo --snapshot-type disk
```

A live snapshot leaves a running QEMU sandbox available, but it must be a disk snapshot:

```bash
smolvm sandbox snapshot create demo --snapshot-type disk --resume-source --live-only
```

## Current limits

Snapshots do not support Windows guests, workspace mounts, extra drives, shared disks, or raw QEMU disks. Snapshot creation can pause a sandbox unless you use the live-only command above.

**Implementation notes:** snapshot types and their meanings are defined in [`src/smolvm/types.py`](../../src/smolvm/types.py); support checks and restore behavior are in [`src/smolvm/vm.py`](../../src/smolvm/vm.py); the facade flushes a guest before a disk snapshot in [`src/smolvm/facade.py`](../../src/smolvm/facade.py). See [`tests/test_snapshot.py`](../../tests/test_snapshot.py) and [`tests/test_snapshot_qemu.py`](../../tests/test_snapshot_qemu.py).
