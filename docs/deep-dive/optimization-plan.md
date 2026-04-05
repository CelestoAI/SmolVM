# Production Concurrency Optimization Plan

## Context

SmolVM's current architecture is fundamentally synchronous — zero async/threading primitives in the VM lifecycle. This creates a hard ceiling of ~10–20 concurrent VM starts before serialization dominates. At 50–100 concurrent VMs, wall-clock startup time reaches 8–15 minutes.

---

## Bottleneck Summary (worst first)

| Bottleneck | Root Cause | Impact at 50 VMs |
|---|---|---|
| Disk materialization | Full `shutil.copy2` / `qemu-img convert` per VM | 100–250s |
| SQLite exclusive locks | `EXCLUSIVE` transactions on IP/port allocation | 50s+ serialized |
| Fixed resource pools | 253 IPs, 800 SSH ports, O(n²) linear search | Hard ceiling |
| nftables setup | ~6 subprocess calls per VM, sequential | ~60s |
| Hypervisor boot wait | Blocking socket poll, no multiplexing | ~250s (50 × 5s) |

---

## Implementation Phases

### Phase 1: Block-Level Copy-on-Write Rootfs -- IMPLEMENTED

**Problem it solves:** Disk materialization is the single biggest bottleneck. Every VM start copies 512MB–4GB synchronously. At 50 VMs this is 100–250 seconds of pure I/O before a single VM has booted.

**Design note:** Kernel-level overlayfs was the initial plan, but both Firecracker and QEMU require a block device *file* (not a directory). The solution uses block-level CoW mechanisms native to each backend instead.

**Solution implemented:**

**QEMU — thin qcow2 overlay (near-instant, near-zero disk):**
```
rootfs.ext4 (shared, read-only base image)
      ↓  backing file reference
 {vm_id}.qcow2 (thin overlay — only stores writes)
      ↓
 QEMU -drive file={vm_id}.qcow2
```
`qemu-img create -f qcow2 -b rootfs.ext4 -F raw {vm_id}.qcow2` — creates a thin overlay backed by the shared base image. Reads miss the overlay and fall through to the base. Writes go to the overlay only. Creation is near-instant regardless of base image size.

**Firecracker — reflink copy (instant on btrfs/XFS, fallback on ext4):**
```
rootfs.ext4 (shared base)
      ↓  cp --reflink=auto
 {vm_id}.ext4 (CoW clone on supported filesystems)
      ↓
 Firecracker add_drive({vm_id}.ext4)
```
`cp --reflink=auto` uses filesystem-level CoW on btrfs and XFS (instant, zero copy). On ext4 or macOS it falls back to a regular copy (no regression from previous behavior).

**Snapshot handling:** QEMU snapshots now flatten the overlay into a standalone qcow2 via `qemu-img convert`, so snapshot artifacts have no backing-file dependency.

**Files changed:**
- `vm.py` — `_materialize_rootfs()` now calls `_create_qemu_overlay_disk()` (QEMU) or `_copy_with_reflink()` (Firecracker)
- `vm.py` — added `_create_qemu_overlay_disk()` and `_copy_with_reflink()` methods
- `runtime_qemu.py` — added `_copy_disk_standalone()` to flatten overlays during snapshot creation
- `tests/test_vm_qemu.py`, `tests/test_snapshot_qemu.py` — updated mocks

**Impact:**
- QEMU: VM start disk materialization drops from 2–5s (qemu-img convert) to <100ms (qemu-img create overlay)
- Firecracker on btrfs/XFS: drops from seconds of I/O to near-instant reflink
- Firecracker on ext4: no change (fallback to regular copy)
- No regressions — all 485 tests pass

---

### Phase 2: S3-Backed Image Registry (follows naturally from Phase 1) -- IMPLEMENTED (Phase 2a)

**Problem it solves:** In a multi-host fleet, every machine needs the rootfs image locally. Manual distribution doesn't scale. A 4GB browser image shipped to 20 hosts is 80GB of transfers per image update.

**Solution implemented (Phase 2a — download-and-cache):**

Images are stored in S3 as a prefix containing a `smolvm-image.json` manifest plus the kernel, rootfs, and optional initrd. SmolVM downloads and caches them locally on first use, then the existing CoW overlay layer (Phase 1) takes over.

```
S3 bucket
  └─ images/alpine-ssh/
       ├─ smolvm-image.json   (manifest: filenames + SHA-256 hashes)
       ├─ vmlinux.bin          (kernel)
       └─ rootfs.ext4          (rootfs)
           ↓  download + cache locally
      ~/.smolvm/images/s3/<cache-key>/
           ↓  qcow2 overlay / reflink copy (Phase 1)
      per-VM isolated disk
```

**CLI:**
```bash
smolvm create --image s3://bucket/images/alpine-ssh/
smolvm create --image s3://bucket/images/alpine-ssh/ --name my-vm --memory-mib 1024
```
`--image` and `--os` are mutually exclusive.

**SDK:**
```python
vm = SmolVM(image="s3://bucket/images/alpine-ssh/")
```

**S3 client:** `boto3` as optional dependency (`pip install 'smolvm[s3]'`). Auth uses boto3's standard credential chain (env vars, `~/.aws/credentials`, IAM roles).

**Files changed:**
- `images.py` — `S3ImageRef`, `S3ImageManifest`, `parse_s3_image_uri()`, `ImageManager.ensure_s3_image()`, `_download_s3_file()`
- `facade.py` — `_build_s3_image_config()`, `SmolVM.__init__()` gains `image` parameter
- `cli.py` — `--image` flag in mutually exclusive group with `--os`
- `pyproject.toml` — `[project.optional-dependencies] s3 = ["boto3>=1.26"]`
- `__init__.py` — exports `S3ImageManifest`, `S3ImageRef`

**Design decisions:**
- `VMConfig` stays unchanged — S3 URIs resolve to local paths *before* config construction
- Manifest-based discovery — single URI, self-describing, extensible
- Download-and-cache first (not FUSE) — works on all platforms, no system dependencies

**Phase 2b (future):** FUSE mount (`mountpoint-s3`) for lazy block-level loading. The architecture leaves a clean seam — `ensure_s3_image()` returns local paths today, a future `mount_s3_image()` would return FUSE mount paths. The rest of the pipeline works unchanged.

---

### Phase 3: Database — Configurable Backend (SQLite default, PostgreSQL for production)

**Current problem:** Every IP allocation, port reservation, and VM state write uses `isolation_level="EXCLUSIVE"` with a full table scan. One VM at a time can allocate resources.

Also, resource pools are hardcoded small:
- 253 IPs (`172.16.0.2–254`) — uses a `/24` slice of a `/16` for no reason
- 800 SSH ports (`2200–2999`)
- O(n²) behavior: linear scan inside exclusive lock, repeated for every concurrent VM

**Approach:** Support both SQLite and PostgreSQL via a `SMOLVM_DATABASE_URL` environment variable (DSN). SQLite remains the default for development and single-host use. For production fleet deployments, users set the DSN to PostgreSQL.

- **Default (no env var):** SQLite at `~/.local/state/smolvm/smolvm.db` — zero config, works today
- **Production:** `SMOLVM_DATABASE_URL=postgresql://user:pass@host/smolvm` — unlocks MVCC, row-level locking, multi-host

**What PostgreSQL unlocks:**
- Multi-host deployments (shared DB across fleet nodes)
- Connection pooling (`pgbouncer` or SQLAlchemy pool)
- Proper IPAM via `SELECT ... FOR UPDATE SKIP LOCKED` — concurrent allocation without serialization
- No artificial resource pool ceilings

**What it doesn't fix:** Async lifecycle. The DB serialization problem goes away, but disk I/O, hypervisor boot, and nftables subprocess calls are still synchronous — that's Phase 4.

**Migration scope:**
- `storage.py` — abstract behind a DB interface; add PostgreSQL driver (`psycopg2`/`asyncpg` or SQLAlchemy)
- Schema migration tooling (Alembic or similar)
- Connection string resolution from `SMOLVM_DATABASE_URL` env var

---

### Phase 4: Async VM Lifecycle

**Problem it solves:** Even with fast DB and no-copy rootfs, the startup pipeline is synchronous. Booting 50 VMs means waiting for VM 1 to finish before starting VM 2.

**What needs async:**
- Hypervisor boot wait (currently blocks on socket poll) — highest value
- nftables subprocess calls — `asyncio.create_subprocess_exec` instead of `subprocess.run`
- Parallel VM starts via `asyncio.gather()`

**Scope:** Core lifecycle methods in `vm.py` (`start`, `stop`, `create`) need `async def`. The facade (`facade.py`) exposes sync wrappers for backwards compatibility.

---

## Recommended Order

```
Phase 1: Overlayfs (local, no infra needed, biggest single win)
   ↓
Phase 2: S3 image registry (fleet distribution, builds on overlayfs)
   ↓
Phase 3: Configurable DB — SQLite default, PostgreSQL via SMOLVM_DATABASE_URL
   ↓
Phase 4: Async lifecycle (enables true concurrency)
```

Phases 2–4 can be parallelized depending on team capacity.

---

## Hard Limits to Remove Regardless of Phase

These are low-effort fixes that should be done early:

- IP pool: change `IP_POOL_END = 254` → use full `/16` (`172.16.0.0/16`, 65k addresses) in `storage.py`
- SSH port pool: expand `SSH_PORT_END` or eliminate SSH port forwarding requirement for Firecracker (VMs have direct IPs)
- nftables: batch all rules for a VM into a single `nft` invocation instead of 6 separate subprocess calls

---

## Key Files by Phase

| Phase | Primary Files |
|---|---|
| 1 (Overlayfs) | `vm.py:370–400`, `runtime_firecracker.py:63–71`, `runtime_qemu.py` |
| 2 (S3) | `types.py:91–212`, `vm.py:338–400`, `images.py` |
| 3 (DB) | `storage.py` (full file) |
| 4 (Async) | `vm.py:724–790`, `facade.py`, `runtime_firecracker.py`, `runtime_qemu.py` |
