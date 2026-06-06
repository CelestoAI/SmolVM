# Custom Dockerfile-backed boot image API plan

This file tracks the temporary implementation plan for issue #341. Delete this file after all phases are complete.

## Design principles

- Make custom Dockerfile-backed direct-kernel images a first-class SDK flow.
- Keep downstream callers away from SmolVM internals such as kernel asset names, backend-specific boot arguments, cache layout, and per-VM disk paths.
- Do not inject SmolVM's guest agent into generic custom images unless the caller asks for it.
- Separate rootfs building from backend/kernel/boot resolution so kernel changes do not force Docker rebuilds.
- Keep user-facing errors short, plain, and actionable.

## Phase 1 — public kernel and boot APIs

- Add `smolvm.kernels.ensure_base_kernel_for_backend(backend, arch="host", cache_dir=None)`.
- Add a public `DirectKernelBoot` model that renders backend-correct kernel arguments.
- Preserve current backend quirks:
  - Firecracker/libkrun use the ELF kernel format; QEMU uses the Image/bzImage format.
  - Firecracker gets `pci=off`; QEMU/libkrun do not.
  - QEMU arm64 uses `console=ttyAMA0`; x86_64 uses `console=ttyS0`.
  - Safe boot trims (`tsc=reliable`, `no_timer_check`, and default `quiet`) remain enabled by default.
- Export the new APIs from `smolvm` and `smolvm.images` where appropriate.
- Add unit tests for kernel selection and boot argument rendering.

## Phase 2 — `BootImage`

Goal: add a small public type that describes a bootable base image without creating or launching a VM.

Implementation plan:

- Add `BootImage` in `src/smolvm/images/boot.py` next to `DirectKernelBoot` so boot-related SDK types stay together.
- Add a minimal `FirmwareBoot` marker type for images whose kernel is inside the disk and QEMU boots through firmware.
- Use a frozen Pydantic model for `BootImage`, matching the existing SDK models in `types.py` and `images/manager.py`.
- Fields:
  - `name: str`
  - `rootfs_path: Path`
  - `rootfs_format: Literal["raw-ext4", "qcow2"]`
  - `kernel_path: Path | None = None`
  - `initrd_path: Path | None = None`
  - `boot: DirectKernelBoot | FirmwareBoot | None = None`
  - `boot_args: str | None = None`
  - `backend: Literal["firecracker", "qemu", "libkrun"] | None = None`
  - `arch: Literal["amd64", "arm64"] | None = None`
  - `ssh_capable: bool = False`
- Validation:
  - `name` and `boot_args` must not be blank.
  - `rootfs_path`, `kernel_path`, and `initrd_path` must point to existing files when present.
  - `boot` and `boot_args` are mutually exclusive; `boot_args` is an explicit override path, not a second source of truth.
  - Direct-kernel images need either `boot` or `boot_args`; `kernel_path` may still be `None` because Phase 4 can resolve the base kernel from backend/arch.
  - Firmware images must not set `kernel_path`, `initrd_path`, or `boot_args`.
- Add helper methods/properties now to keep Phase 4 small:
  - `boot_mode` property returning `"direct_kernel"` or `"firmware"`.
  - `render_boot_args(backend, arch)` returning explicit `boot_args`, rendered `DirectKernelBoot`, or `""` for firmware.
- Export `BootImage` and `FirmwareBoot` from `smolvm.images` and top-level `smolvm`.
- Tests:
  - valid direct-kernel image with `DirectKernelBoot`.
  - valid direct-kernel image with explicit `boot_args`.
  - direct-kernel image can omit `kernel_path` for later resolution.
  - reject both `boot` and `boot_args`.
  - reject missing direct-kernel boot information.
  - valid firmware image and firmware invariants.
  - path validation and top-level exports.

## Phase 3 — generic `DockerRootfsBuilder`

Goal: build and cache a Dockerfile-backed raw ext4 rootfs without forcing callers through SmolVM's SSH image builder.

Implementation plan:

- Add `DockerRootfsBuilder` in `src/smolvm/images/builder.py` and export it from `smolvm` and `smolvm.images`.
- Accept Dockerfile text, build context entries, rootfs size, build args, cache directory, Docker platform override, fingerprint inputs, and `ssh_capable` metadata.
- Reuse existing Docker build/export and ext4 creation helpers, but do not call `ImageBuilder._do_build()` because that injects SmolVM's guest agent into every image.
- Support context entries as `Path`, `str`, or `bytes`; reject absolute paths, path traversal, and `Dockerfile` collisions inside the build context.
- Cache rootfs artifacts under `~/.smolvm/images/custom/<name>/<fingerprint>/rootfs.ext4`.
- Fingerprint only rootfs build inputs: Dockerfile hash, context content hashes, rootfs size, build args, target platform/arch, and user fingerprint inputs. Do not include backend or selected kernel identity, so Firecracker/QEMU can reuse the same rootfs for the same arch.
- Add file locking around cache misses so concurrent callers do not race the same build.
- Have `ensure()` resolve the backend/arch kernel and return a `BootImage` with rendered boot metadata supplied by the caller.
- Tests:
  - build path returns a `BootImage`.
  - cache hit avoids a rebuild.
  - QEMU and Firecracker reuse the same rootfs when the rootfs fingerprint is unchanged.
  - unsafe context paths and missing context files fail before Docker work starts.
  - exports are available at top level and `smolvm.images`.

## Phase 4 — `SmolVM.from_image()`

- Add a high-level constructor that consumes `BootImage` and builds the corresponding `VMConfig` internally.
- Resolve backend, arch, kernel path, and boot args when missing.
- Expose consistent knobs: `vm_id`, `vcpus`, `memory_mb`, `network`, port forwards, vsock, comm channel, disk mode, SSH capability, and readiness behavior.
- Keep no-SSH custom images valid; do not assume command execution is available unless the image declares a supported control path.

## Phase 5 — per-VM disk resize/grow

- Move disk sizing into SmolVM's VM lifecycle so callers never read `vm.info.config.rootfs_path` to resize disks.
- Add pre-start or create-time options for requested disk size and filesystem growth.
- Always resize the isolated per-VM disk, never the shared base image.
- Raw ext4 path: `truncate`, `e2fsck -fy`, then `resize2fs`.
- qcow2 path: `qemu-img resize`; only claim filesystem growth when SmolVM can actually grow the guest filesystem.
- Be explicit about QEMU raw-ext4 overlays: either materialize a growable raw disk or reject unsupported grow combinations with a clear error.

## Final cleanup

- Delete `plan.md` after all phases are implemented and accepted.
