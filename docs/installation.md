# Installation

Most people install SmolVM with the one-liner from the [README](../README.md#quickstart) and never need to read further. This page is for the cases where you want more control over how SmolVM gets onto a machine — for example, baking it into a golden image, or pinning the exact Firecracker version your AMI ships with.

## Standard install

The simplest path:

```bash
pip install smolvm
smolvm setup
smolvm doctor
```

`smolvm setup` installs Firecracker, configures host networking permissions, and confirms the machine is ready to run sandboxes. It assumes the machine already has `/dev/kvm` (i.e. it is the host where sandboxes will run).

## Bake-time install (golden AMI / two-stage deploys)

Some teams want to pre-install everything SmolVM needs into a base image, so that booting a fresh machine becomes "boot the AMI, accept secrets, start serving traffic" rather than "boot, then spend several minutes installing." This is common on AWS where you bake the image on a cheap builder (e.g. `c5.large`) and run it on an expensive metal instance (e.g. `c5.metal`) where KVM is actually available.

The challenge is that the builder doesn't have `/dev/kvm`, so the default `smolvm setup` refuses to run. **Bake mode** is the answer:

```bash
smolvm setup --for-bake --runtime-user ubuntu
```

`--for-bake` skips the install-time checks that depend on the live runtime environment:

- The `/dev/kvm` device check (the builder doesn't have one yet).
- The post-install sudoers self-test (the builder may not have networking tools warmed up).

Once the AMI boots on a real KVM host, finish setup by running:

```bash
smolvm doctor
```

`smolvm doctor` verifies that `/dev/kvm` is present, that Firecracker is on the path, and that the sudoers rules actually grant the runtime user the access they need. **You should treat `smolvm doctor` as a required first-boot step** — it is the safety net that catches anything bake mode skipped.

### Pinning the Firecracker version

When you bake an AMI, you usually want to know exactly which Firecracker version it ships with, independent of which SmolVM version you happen to be installing. Pass a flag or set an environment variable:

```bash
smolvm setup --firecracker-version v1.14.1
```

```bash
SMOLVM_FIRECRACKER_VERSION=v1.14.1 smolvm setup
```

The flag takes precedence over the env var. Both override SmolVM's built-in default. The legacy `FC_VERSION` env var still works for backwards compatibility, but new pipelines should use `SMOLVM_FIRECRACKER_VERSION`.

### Locating the packaged shell scripts

If your bake pipeline needs to invoke SmolVM's setup scripts directly (e.g. from a Packer provisioner), use the stable CLI to find them:

```bash
SMOLVM_ASSETS="$(smolvm setup --assets-dir)"
bash "${SMOLVM_ASSETS}/internal/configure-runtime-sudoers.sh" --runtime-user ubuntu
```

This avoids reaching into the Python package layout — `smolvm setup --assets-dir` is the supported interface and won't change between releases. Don't import `from smolvm.host.setup import packaged_asset_root`; that's an internal helper.

### Granular escape hatches

`--for-bake` is shorthand for the common bake-mode case. If you need finer control, the underlying flags are:

- `--skip-kvm-check` — install Firecracker without requiring `/dev/kvm`.
- `--skip-runtime-check` — install the sudoers rule without running the post-install self-test.

You can combine them with the other `smolvm setup` flags as you need.

### What can still go wrong at runtime

Bake mode trades install-time strictness for portability. Two failure modes are worth knowing about:

1. **Binary path differences between the builder and the runtime host.** The sudoers file records the exact location of `nft`, `ip`, and friends as they were found on the builder. If the runtime host installs them in different folders, the sudoers rule silently won't match. `smolvm doctor` is the place this surfaces — run it on first boot, before any sandbox starts.
2. **Missing tools surface later.** Without the install-time self-test, a missing `nftables` package shows up the first time a sandbox tries to set up networking, not during install. Again, `smolvm doctor` catches this earlier.

Both are reasons to treat `smolvm doctor` as a hard gate at first boot — fail closed if it returns a non-zero exit code.
