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

### Phase 1: Overlayfs + No-Copy Rootfs (do first)

**Problem it solves:** Disk materialization is the single biggest bottleneck. Every VM start copies 512MB–4GB synchronously. At 50 VMs this is 100–250 seconds of pure I/O before a single VM has booted.

**Solution:** Mount a shared read-only base image and give each VM a writable overlay layer. Zero copy on start.

```
rootfs.ext4 (shared, read-only base)
      ↓
 overlayfs
      ↓
 per-VM upper dir (writable, local, ephemeral)
```

**What changes:**
- `_materialize_rootfs()` (`vm.py:370`) — replace `shutil.copy2` with overlayfs mount
- Per-VM "disk" becomes a small upper-dir folder, not a full image copy
- VM deletion just unmounts and removes the upper dir
- `disk_mode="isolated"` naturally maps to this model; `disk_mode="shared"` becomes the same thing

**Impact:** VM start drops from seconds of I/O to milliseconds of mount setup. The entire disk materialization bottleneck is eliminated.

**Why before S3:** Overlayfs is the local mechanism. S3 becomes the source of the read-only base later — the two stack naturally. Doing S3 without overlayfs first would mean downloading full images per VM, which is worse than the current copy.

---

### Phase 2: S3-Backed Image Registry (follows naturally from Phase 1)

**Problem it solves:** In a multi-host fleet, every machine needs the rootfs image locally. Manual distribution doesn't scale. A 4GB browser image shipped to 20 hosts is 80GB of transfers per image update.

**Solution:** Store canonical images in S3. Mount via FUSE (`mountpoint-s3` or `s3fs`) as the read-only base layer for overlayfs. VMs only fetch the blocks they actually read — lazy loading.

```
S3 bucket (image registry)
      ↓
 FUSE mount (mountpoint-s3, read-only)
      ↓
 overlayfs (per-VM writable upper dir)
      ↓
 Firecracker / QEMU
```

**What changes on top of Phase 1:**
- `VMConfig.rootfs_path` needs to accept a URI (`s3://bucket/images/alpine-ssh/rootfs.ext4`) or an abstract `StorageRef` type alongside local `Path`
- `_materialize_rootfs()` gains a "resolve" step: if source is remote, ensure FUSE mount exists before overlayfs setup
- Snapshot artifacts (`disk.ext4`, `disk.qcow2`) would optionally push to S3
- Image versioning is free via S3 object versioning

**Code touch points:**
- `types.py` — `VMConfig.rootfs_path`, `kernel_path`, `extra_drives`
- `vm.py` — `_materialize_rootfs()`, `_instance_disk_path()`
- `images.py` — image resolution and caching layer
- `storage.py` — snapshot artifact paths need URI support

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
