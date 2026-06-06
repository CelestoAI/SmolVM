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

- Add a public `BootImage` type representing a bootable base image, not a running VM.
- Include rootfs/kernel/initrd paths, rootfs format, backend, arch, boot profile, optional boot-arg override, and SSH capability metadata.
- Validate path existence and reject ambiguous `boot` + `boot_args` combinations unless an override rule is explicit.
- Keep the type usable by Docker builders now and future catalogs later.

## Phase 3 — generic `DockerRootfsBuilder`

- Add a public builder that accepts Dockerfile text, build context entries, rootfs size, build args, cache directory, and fingerprint inputs.
- Reuse the existing Docker build/export and ext4 creation helpers, but remove implicit SmolVM guest-agent injection for generic images.
- Support context entries as `Path`, `str`, or `bytes`; reject absolute paths and path traversal inside the build context.
- Cache rootfs artifacts under a stable content fingerprint that includes Dockerfile, context content, rootfs size, build args, target platform/arch, and user fingerprint inputs.
- Add file locking around cache misses so concurrent callers do not race the same build.
- Have `ensure()` resolve the backend/arch kernel and boot args, then return a `BootImage`.

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
