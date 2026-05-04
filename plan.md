# Plan: End-to-end published-image flow for macOS

Tracking doc for the QEMU/libkrun kernel rollout. Optimizing for **time-to-end-to-end-working**, not polish. Anything marked _nice-to-have_ is deferred until the macOS path actually boots.

## Done = this command works on a clean macOS arm64

```sh
SMOLVM_USE_PUBLISHED=1 smolvm openclaw start
```

It should: download a kernel + rootfs from GH Releases, decompress, boot QEMU, SSH ready, run openclaw. Linux behavior unchanged.

## Status

| # | PR | What | Status |
|---|---|---|---|
| α | [#247](https://github.com/CelestoAI/SmolVM/pull/247) | Schema: `vmm` dimension on PublishedImage | ✅ **merged** |
| β | [#248](https://github.com/CelestoAI/SmolVM/pull/248) | Custom QEMU kernel build (`kernel/qemu/*` + `build-qemu-kernel.yml`) | 🔄 **open** |
| ⚙ | — | Trigger `build-qemu-kernel.yml` once → kernel artifacts on `images-v0.0.13` draft release | ⏳ pending β merge |
| γ | — | Manifest QEMU rows + CLI `_vmm_for_host` (Linux→firecracker, Darwin→qemu). Supersedes #246. | ⏳ to write |
| 📤 | — | Publish the `images-v0.0.13` draft release (one click) | ⏳ pending γ merge + manual smoke |
| ✅ | — | Smoke test: `SMOLVM_USE_PUBLISHED=1 smolvm openclaw start` on macOS | ⏳ |

## Critical path (fast)

1. **Merge #248 (PR β).** No behavior change; safe to land.
2. **Trigger `build-qemu-kernel.yml`** for both arches. Verify artifacts appear on the draft release.
3. **Open PR γ** with the manifest rows pointing at the new kernel artifacts (SHAs from step 2's run summary) + CLI `_vmm_for_host()` swap. **Replaces** [#246](https://github.com/CelestoAI/SmolVM/pull/246) (the macOS hard-reject) — close #246 as superseded when γ lands.
4. **Smoke test locally** on the user's Mac: download the artifacts via the new path, verify they boot under QEMU.
5. **Publish the draft release** to make the URLs resolve for everyone.
6. **Final smoke test:** `SMOLVM_USE_PUBLISHED=1 smolvm openclaw start` on a clean macOS box. Done.

Best-case timeline if nothing surprises: merge β → ~15–30 min CI build → write γ → smoke locally → publish → done. Probably half a day of focused work + waiting on CI.

## Decisions made for speed (revisit later)

- **Skip PR ε (boot smoke gate).** A CI workflow that boots both kernel variants under QEMU+KVM on Linux runners would be ideal, but it's a quality gate, not a feature. Defer until end-to-end works; add as defense once we have one.
- **Skip PR δ (libkrun stub rows).** Optional; landing it doesn't change user behavior since `_vmm_for_host()` doesn't return `"libkrun"`. Defer.
- **Don't merge PR #246 first.** PR γ replaces its hard-reject directly with the platform-aware logic. Less churn. Close #246 as superseded when γ lands.
- **Keep `SMOLVM_USE_PUBLISHED=1` opt-in for now.** Don't flip the default in this rollout — that's a separate decision after the macOS path is confirmed working in the wild.
- **Asymmetric kernel naming** (`vmlinux-<arch>-qemu.bin` for new kernels vs `<preset>-<arch>-vmlinux.bin` for existing Firecracker assets). Documented in `kernel/qemu/README.md`. Don't normalize until needed.
- **Custom kernel maintenance.** Pinned to Linux 6.12.10 LTS. CI cache makes re-builds free until inputs change. Live with the burden; it's small.

## Nice-to-haves (deferred)

- **PR ε:** boot smoke gate workflow that proves the kernels actually boot on Linux before we point users at them.
- **PR δ:** libkrun stub manifest rows (reuse the QEMU kernel since libkrun consumes the same shape).
- **Default flip:** `SMOLVM_USE_PUBLISHED=1` becomes implicit. Wait until field reports show the path is solid.
- **Kernel signing:** cosign keyless on the kernel artifacts. Track alongside rootfs signing.
- **Naming normalization:** rename Firecracker assets to `vmlinux-<arch>-firecracker.bin` to match the new convention. Breaking change to existing manifest URLs; defer until a natural opportunity (e.g. major version bump).
- **Initrd support in `PublishedImage`.** Not needed today since both our kernels are no-modules. Add when a future preset requires it.
- **Cleanup of orphaned cache directories.** Old layout (without `-<vmm>` suffix) sits unused on existing users' disks. Leave it; users can `rm -rf` when they care.
- **Investigate libkrun parity for macOS.** If libkrun boots our QEMU kernel cleanly, macOS users get faster cold-start than QEMU. ~30-min spike. Defer until end-to-end works.

## Files touched / to touch

| Layer | Files |
|---|---|
| Schema (done in α) | `src/smolvm/images/published.py`, `tests/test_published_images.py` |
| Kernel build (done in β) | `kernel/qemu/{linux.version, linux.sha256, config.fragment, build.sh, README.md}`, `.github/workflows/build-qemu-kernel.yml` |
| Manifest rows + CLI flip (γ — to do) | `src/smolvm/images/published.py` (add 2 QEMU rows), `src/smolvm/cli/main.py` (add `_vmm_for_host()`, replace any macOS-specific code with the platform-aware logic), `tests/test_cli.py` |

## Notes

- The plan in `~/.claude/plans/yeah-let-s-build-our-buzzing-spindle.md` has the deeper design rationale. This file is the lightweight tracker.
- Update this doc as PRs merge. Move items between "status" and "deferred" as decisions change.
