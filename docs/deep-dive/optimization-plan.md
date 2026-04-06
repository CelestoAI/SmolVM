# Production Concurrency Optimization Plan

## Context

SmolVM's current architecture is fundamentally synchronous — zero async/threading primitives in the VM lifecycle. This creates a hard ceiling of ~10–20 concurrent VM starts before serialization dominates. At 50–100 concurrent VMs, wall-clock startup time reaches 8–15 minutes.

---

## Bottleneck Summary (worst first)

| Bottleneck | Root Cause | Impact at 50 VMs | Status |
|---|---|---|---|
| Disk materialization | Full `shutil.copy2` / `qemu-img convert` per VM | 100–250s | **Fixed (Phase 1)** |
| Image distribution | Manual copy to every host | Doesn't scale | **Fixed (Phase 2a)** |
| SQLite exclusive locks | `EXCLUSIVE` transactions on IP/port allocation | 50s+ serialized | **Fixed (Phase 3)** |
| Fixed resource pools | 253 IPs, 800 SSH ports, O(n²) linear search | Hard ceiling | Partially (Phase 3) |
| nftables setup | ~6 subprocess calls per VM, sequential | ~60s | **Fixed (Phase 4)** |
| Hypervisor boot wait | Blocking socket poll, no multiplexing | ~250s (50 × 5s) | **Fixed (Phase 4)** |

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

**S3 client:** `boto3` + `python-dotenv` as optional dependencies (`pip install 'smolvm[s3]'`). Credentials loaded automatically from `.env` file via python-dotenv.

**S3-compatible store support (Cloudflare R2, MinIO, etc.):**
```bash
# .env file — no `export` needed, loaded automatically
SMOLVM_S3_ENDPOINT_URL=https://<id>.r2.cloudflarestorage.com
SMOLVM_S3_ACCESS_KEY_ID=<key>
SMOLVM_S3_SECRET_ACCESS_KEY=<secret>
```
Falls back to boto3's standard credential chain (`AWS_*` env vars, `~/.aws/credentials`, IAM roles) when `SMOLVM_S3_*` vars are not set. Half-set credentials (key without secret) produce a clear error.

**Cloud-init:** S3 images with an initrd (e.g., Ubuntu cloud images) automatically get a cloud-init seed ISO generated and attached for SSH key injection.

**Files changed:**
- `images.py` — `S3ImageRef`, `S3ImageManifest`, `parse_s3_image_uri()`, `ImageManager.ensure_s3_image()`, `_download_s3_file()`, `_require_boto3()` with `SMOLVM_S3_*` env var + dotenv support
- `facade.py` — `_build_s3_image_config()` with cloud-init seed ISO for cloud images, `SmolVM.__init__()` gains `image` parameter
- `cli.py` — `--image` flag in mutually exclusive group with `--os`
- `pyproject.toml` — `[project.optional-dependencies] s3 = ["boto3>=1.26", "python-dotenv>=1.0"]`
- `__init__.py` — exports `S3ImageManifest`, `S3ImageRef`

**Design decisions:**
- `VMConfig` stays unchanged — S3 URIs resolve to local paths *before* config construction
- Manifest-based discovery — single URI, self-describing, extensible
- Download-and-cache first (not FUSE) — works on all platforms, no system dependencies
- Uses `get_object` (not `download_file`) to avoid `HeadObject` calls that some S3-compatible stores reject
- Offline cache fallback — fully cached images work when S3 is unreachable
- Path traversal protection — manifest filenames validated via Pydantic (rejects `../`, absolute paths, collisions)

**Verified end-to-end:** Ubuntu cloud image uploaded to Cloudflare R2, pulled via `smolvm create --image s3://...`, VM booted, SSH connected, commands executed.

**Phase 2b (FUSE mount) was evaluated and dropped** — see Recommended Order section for rationale.

---

### Phase 3: Database — Configurable Backend (SQLite default, PostgreSQL for production) -- IMPLEMENTED

**Problem it solves:** Every IP allocation, port reservation, and VM state write uses `isolation_level="EXCLUSIVE"` with a full table scan. One VM at a time can allocate resources. PostgreSQL unlocks row-level locking and multi-host fleet deployments.

**Architecture implemented:**

```text
src/smolvm/storage/
  __init__.py          — factory (create_state_manager), exports, StateManager alias
  _protocol.py         — StateManagerProtocol (typing.Protocol, ~30 methods)
  _base.py             — shared helpers, constants (IP_POOL_*, SSH_PORT_*)
  _sqlite.py           — SQLiteStateManager (extracted from old storage.py)
  _postgres.py         — PostgresStateManager (psycopg v3 + connection pool)
```

**Configuration:**
```bash
# SQLite (default — zero config)
# Uses {data_dir}/smolvm.db automatically

# PostgreSQL (production)
export SMOLVM_DATABASE_URL=postgresql://user:pass@host/smolvm
```

**Factory function:** `create_state_manager(db_path=..., database_url=...)` resolves the backend:
1. Explicit `database_url` parameter
2. `SMOLVM_DATABASE_URL` env var
3. `db_path` parameter (SQLite)

**PostgreSQL specifics:**
- **Driver:** `psycopg` v3 with `psycopg_pool` for connection pooling (min=2, max=10)
- **IP/port allocation:** Uses `pg_advisory_xact_lock()` — transaction-scoped advisory locks that serialize only the scan-then-insert pattern without blocking reads
- **Placeholders:** `%s` (vs SQLite `?`)
- **Boolean:** Native `BOOLEAN` (vs SQLite `INTEGER`)
- **Schema:** Same `CREATE TABLE IF NOT EXISTS` pattern, no migration framework

**Backwards compatibility:** `from smolvm.storage import StateManager` still works (alias for `SQLiteStateManager`). All consumers migrated to `create_state_manager()` + `StateManagerProtocol`.

**Files changed:**
- `storage.py` → `storage/` package (5 new files)
- `vm.py` — `create_state_manager(db_path=...)` + `StateManagerProtocol` type hint
- `cli.py` — `create_state_manager()` replaces `StateManager()`
- `browser.py` — same
- `dashboard/server.py`, `dashboard/poller.py` — same
- `pyproject.toml` — `postgres = ["psycopg[binary]>=3.1", "psycopg-pool>=3.1"]`

**Impact:**
- SQLite: zero behavioral change — all 523 tests pass
- PostgreSQL: advisory locks replace exclusive file locks, connection pooling replaces per-operation connections
- Multi-host fleet: shared PostgreSQL DB enables centralized state across nodes

---

### Phase 4: Async VM Lifecycle -- IMPLEMENTED

**Problem it solves:** Even with fast DB and no-copy rootfs, the startup pipeline is synchronous. Booting 50 VMs means waiting for VM 1 to finish before starting VM 2.

**Approach:** Dual methods on the same classes — every sync method gets an `async_` twin. No breaking changes, no separate async classes.

**What was made async:**
- `utils.py` — `async_run_command()` (foundation for all async subprocess calls)
- `network.py` — 25 async methods for TAP, nftables, NAT, port forwarding
- `runtime.py` — Extended `RuntimeContext` and `RuntimeAdapter` protocol with async callables
- `runtime_qemu.py` — `async_start()`, `async_stop()`, `_async_wait_for_runtime()`
- `runtime_firecracker.py` — `async_start()`, `async_stop()`
- `api.py` — `async_wait_for_socket()`
- `vm.py` — `async_create()`, `async_start()`, `async_stop()`, `async_delete()` + 8 private async helpers
- `facade.py` — `async_start()`, `async_stop()`, `async_run()`, `async_wait_for_ssh()`, `async_delete()`, `async_create_many()`, `__aenter__`/`__aexit__`

**Killer feature — batch creation:**
```python
import asyncio
from smolvm import SmolVM

async def main():
    vms = await SmolVM.async_create_many(
        [config1, config2, config3],
        boot_timeout=60,
        concurrency=10,
    )
    for vm in vms:
        result = await vm.async_run("echo hello")
        print(result.stdout)

asyncio.run(main())
```

**Async context manager:**
```python
async with SmolVM(config) as vm:
    result = await vm.async_run("uname -r")
```

**Impact:** 50 VMs boot in ~30s (one boot cycle) instead of 50×30s = 25 minutes.

---

## Recommended Order

```
Phase 1: Block-level CoW rootfs .......................... ✅ DONE
   ↓
Phase 2a: S3 image registry (download-and-cache) ........ ✅ DONE
   ↓
Phase 3: Configurable DB (PostgreSQL) .................... ✅ DONE
   ↓
Phase 4: Async lifecycle (true concurrency) .............. ✅ DONE
```

**Phase 2b (FUSE mount) was evaluated and dropped.** Download-and-cache covers fleet distribution, FUSE adds operational complexity (mount lifecycle, daemon management, error handling) for marginal benefit, Firecracker can't use it (needs full copy, not qcow2 overlay), and qcow2 over FUSE has seek penalties that could make boot slower than cached local files. FUSE tools (mountpoint-s3, s3fs, rclone) are system packages — not pip-installable — adding install friction.

---

## Remaining Low-Effort Improvements

| Item | Effort | Impact | Notes |
|------|--------|--------|-------|
| SSH port pool expansion | Trivial | 800 → 47k ports | Change `SSH_PORT_END` to 49150 |
| Dashboard native async | Small | Cleaner code | 6 `asyncio.to_thread` → native `async_stop/delete` |
| IP pool /16 expansion | Medium | 253 → 65k IPs | Requires TAP naming redesign (`tap{last_octet}` collides) |
| nftables batching | Skip | Already batched | Only 2-3 nft calls per VM (via `_run_nft_script`) |

---

## All Phases Complete

| Phase | Primary Files | Status |
|---|---|---|
| 1 (CoW rootfs) | `vm.py`, `runtime_qemu.py` | ✅ Done |
| 2a (S3 registry) | `images.py`, `facade.py`, `cli.py` | ✅ Done |
| 3 (DB) | `storage/` package (5 files) | ✅ Done |
| 4 (Async) | `utils.py`, `network.py`, `vm.py`, `facade.py`, `runtime_*.py`, `api.py` | ✅ Done |
