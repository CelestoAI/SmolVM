# A microVM From First Principles

A book-length tour of how a SmolVM sandbox actually boots — what's in the kernel, what's in the rootfs, how the two meet at PID 1, and how SmolVM stitches it all together. Written so a beginner engineer can follow each step, but with enough depth that someone hacking on the build pipeline can use it as a reference.

The running example throughout is what `smolvm codex start` actually does. We'll build up to it from `mkfs.ext4` and `vmlinux`.

---

## Chapter 1: A Linux VM is just two files

Forget hypervisors and CPU rings for a moment. From the user's perspective, the entire payload of a SmolVM sandbox is two files:

```
~/.smolvm/images/codex-v0.0.14-amd64-qemu/
├── vmlinux.image   ← the kernel (~13 MB)
└── rootfs.ext4     ← the "hard drive" (~150 MB compressed, ~1 GB extracted)
```

That's it. The hypervisor (Firecracker or QEMU) loads the kernel into memory, hands the rootfs to the kernel as its block device, and starts the CPU. Everything else — networking, SSH, the agent CLI — lives inside those two files.

This is the mental model to hold:
- **Kernel** = a binary the CPU executes. It manages hardware (vCPUs, virtual disks, virtual NICs) and presents a userspace API.
- **Rootfs** = a filesystem in a file. To the kernel it looks like an SSD; to userspace it looks like `/`.

When something boots slowly or breaks, it's almost always one of:
1. The kernel can't talk to the rootfs (missing driver, wrong format).
2. The kernel boots but the rootfs's `/init` (or systemd) can't bring up networking / SSH / userspace.
3. Userspace is fine but the host can't reach the guest (port forwarding, firewall).

The chapters that follow zoom into each piece.

---

## Chapter 2: The kernel — two flavors of the same thing

If you peek at SmolVM's release page, you'll see four kernel files per version:

```
vmlinux-amd64.elf        # Firecracker / libkrun
vmlinux-amd64.image      # QEMU
vmlinux-arm64.elf
vmlinux-arm64.image
```

Same Linux source (currently 6.12.85), same Kconfig, same build invocation. **Only the container format on the wire differs.**

### Why two formats?

Linux's build system can emit the kernel in several wrappers:

| Format | What it is | Who consumes it |
|---|---|---|
| `vmlinux` | Raw uncompressed ELF executable | Firecracker, libkrun, anything that does its own ELF loading |
| `bzImage` (x86) / `Image` (arm64) | Linux's "boot protocol" wrapper — a small head that knows how to relocate and decompress itself | QEMU, real bootloaders, anything that follows the documented boot protocol |

Why does this matter? Different VMMs (Virtual Machine Monitors) implement different parts of the loading protocol:

- **Firecracker** is minimalist. It mmaps the ELF, jumps to the entry point, hands the cmdline as ASCII at a known address. If you give it `bzImage`, it errors with `Invalid Elf magic number`.
- **QEMU's aarch64 `virt` machine** does the opposite. Hand it an ELF and you get silent hang — no console output, no error, just nothing. It expects the Linux ARM64 boot protocol (the `Image` format).

So we ship both. The Linux build produces them in a single `make` invocation, costing nothing extra:

```sh
# from kernel/microvm/build.sh, simplified
make ARCH=arm64 vmlinux Image
cp arch/arm64/boot/Image  out/vmlinux-arm64.image
cp vmlinux                out/vmlinux-arm64.elf
```

### Why our own kernel?

Earlier versions of SmolVM pulled kernels from elsewhere:
- Firecracker CI's S3-hosted `vmlinux-5.10.198` (Linux 5.10, frozen 2022)
- Ubuntu cloud images' `vmlinuz-6.x` from `cloud-images.ubuntu.com`

That had three problems:
1. **External CDN dependencies** — Ubuntu's CDN had a slow first-byte that hung `smolvm create` for a user.
2. **Old kernel** — 5.10 missed years of microVM optimizations and CVE patches.
3. **Generic kernel surface** — Ubuntu ships drivers for Bluetooth, USB, sound, etc. None of that exists in a microVM. Dead code = bigger attack surface.

So we build our own. The Kconfig is in [`kernel/microvm/`](../../kernel/microvm/) — a generic-defconfig base plus a small fragment that disables modules (everything is built-in), enables the drivers a microVM actually needs, and turns off cruft.

A few SmolVM-specific Kconfig choices:

```
# CONFIG_MODULES is not set
```
No loadable modules. Everything either built into the kernel or doesn't exist. This means the rootfs has no `/lib/modules` (saves space) and you can't `modprobe` at runtime (forces all needed drivers to be in the build).

```
CONFIG_VIRTIO_MMIO=y
CONFIG_VIRTIO_MMIO_CMDLINE_DEVICES=y
```
Firecracker exposes virtio devices over MMIO (memory-mapped I/O), not PCI. Without these symbols, Firecracker can't enumerate its disk or NIC. QEMU's virt machine uses PCI but having both transports compiled in is harmless.

```
CONFIG_ISO9660_FS=y
```
Cloud-init's NoCloud datasource reads its config from an ISO 9660 disk attached as a virtio-blk device. We don't use cloud-init for the sandbox presets, but the auto-config Ubuntu path still does.

The full list lives in [`kernel/microvm/config.fragment`](../../kernel/microvm/config.fragment). Each line has a `# why:` comment.

---

## Chapter 3: PID 1 — what runs first

When the kernel finishes booting, it has done its job: hardware is up, the rootfs is mounted at `/`, virtual memory works. Now it needs to hand off to userspace.

The way Linux does this is to `exec` exactly one program. That program becomes **PID 1** and is responsible for everything from there: bringing up networking, starting daemons, reaping zombies, eventually shutting the system down.

By default, the kernel looks for `/sbin/init` (or `/init`, `/etc/init`, `/bin/init`, in that order). On a normal Ubuntu system, `/sbin/init` is a symlink to `/lib/systemd/systemd`. systemd is a sprawling service manager that reads unit files, starts services in dependency order, supervises them, etc.

You can override which program PID 1 runs via the kernel command line:

```
init=/init
```

That tells the kernel: don't search the default paths, just `exec /init`.

### Why SmolVM replaces systemd

systemd is wonderful for a long-running server. It's overkill for a sandbox VM that exists to host one agent CLI behind SSH. The cost we don't want:

- **5–15 seconds** of boot time (systemd-network-wait-online, systemd-resolved, journal-flush, …).
- **~80 MB** of disk for systemd binaries + unit files.
- **Complexity** — systemd unit files, dependency cycles, fail loops.

For sandboxes we replace it with a ~100-line shell script. Two such scripts live in this repo:

- [`scripts/ci/preset-init.sh`](../../scripts/ci/preset-init.sh) — used by codex / claude-code / hermes / pi
- A heredoc-generated init in [`builder.py::_base_init_script`](../../src/smolvm/images/builder.py) — used by openclaw

They do six things, in order:

```
1. mount /proc, /sys, /dev, /dev/pts, /run, /tmp
2. parse ip=<guest>::<gw>:<netmask>:... from /proc/cmdline
3. bring up lo + eth0, add address, add default route
4. ssh-keygen -A   (generate host keys if missing)
5. parse smolvm.authorized_key_b64=<base64> from /proc/cmdline
   → decode, write to /root/.ssh/authorized_keys mode 0600
6. exec /usr/sbin/sshd -e
```

Then the script enters an infinite `sleep` loop to keep PID 1 alive. The kernel kills the whole VM when PID 1 exits, so this matters.

```sh
while true; do
    sleep 3600 &
    wait $!
done
```

Why `wait $!` instead of plain `sleep 3600`? Signal delivery. Plain `sleep` blocks signal handlers until it returns; backgrounding it and `wait`ing lets `trap shutdown INT TERM` fire promptly. (Firecracker's `SendCtrlAltDel` reaches PID 1 as SIGINT.)

### Why this works

It works because a sandbox VM has very few requirements:

- **Networking** is virtual and predictable. SmolVM gives every VM the same `10.0.2.15`/`10.0.2.2`/`10.0.2.3` triple via QEMU SLIRP or Firecracker's virtio-net + nft. We don't need DHCP discovery dynamics.
- **Services** beyond sshd run on demand from the SSH session, not at boot. No `nginx.service`, no `cron`, no `systemd-timesyncd`.
- **Logs** go to whatever the SSH session redirects to. No journal.

So PID 1 only needs to: mount the basics, bring up the NIC, start sshd, and stay alive.

### When you'd want systemd back

If you wanted a sandbox that ran multiple coordinated services (a database + a web server + a worker), the dependency-resolution + supervision benefits of systemd would matter. We're not there yet — SmolVM presets are single-process agent harnesses.

---

## Chapter 4: Network from scratch

Picking up the init script in the middle: how does the VM get an IP address?

Two sources of truth:

1. **Kernel command line `ip=` parameter.** The kernel itself can configure the early network from the cmdline. Format:
   ```
   ip=<guest_ip>::<gateway>::<netmask>::<device>:<autoconfig>
   ```
   On SmolVM that's typically:
   ```
   ip=10.0.2.15::10.0.2.2::255.255.255.0::eth0:off
   ```
   "off" means "don't try DHCP/RARP; we're telling you the answer."

2. **Userspace `ip` commands.** The init script also runs `ip link set eth0 up`, `ip addr add`, `ip route add default via`. This is belt-and-braces — the kernel cmdline configures the network *before* userspace runs, but the explicit userspace commands handle the case where the kernel didn't (e.g., the cmdline param wasn't set).

After this, you have:
- `lo` up with `127.0.0.1`
- `eth0` up with the configured guest IP
- A default route pointing at the gateway
- `/etc/resolv.conf` written with `nameserver 8.8.8.8` (or in QEMU's case, the SLIRP-provided `10.0.2.3`)

That's enough for `curl` and SSH and `npm install` to work.

### A subtle bug we currently ship

The init script hardcodes `/24`:

```sh
ip addr add "${GUEST_IP}/24" dev eth0
```

The kernel `ip=` cmdline does include a netmask field at position 4, but we don't parse it. On a non-/24 network, the kernel's auto-configured route works (it used the netmask) but our explicit userspace `ip addr` line gets it wrong. Symptom: hosts on the same subnet are unreachable except via the gateway.

This doesn't bite SmolVM because our virtual networks are always /24. But the openclaw `_base_init_script` has the same hardcode, so the parity fix touches both files. Tracked in issue #275.

---

## Chapter 5: SSH at boot, without cloud-init

Here's the problem the published-image flow had to solve:

> The CI builds **one** rootfs per (preset, arch). It uploads to a release. Later, a user on a different machine downloads it and boots a VM. How does the user's SSH public key get into the guest's `/root/.ssh/authorized_keys`?

Three solutions, in increasing order of complexity:

### Solution A — Bake the key into the rootfs at build time

Don't. This means every user shares the same `authorized_keys`, which means every user can SSH into anyone else's sandboxes. (And anyone who downloads the public rootfs can SSH into anyone's sandboxes.)

### Solution B — Cloud-init NoCloud seed

Cloud-init is Ubuntu's standard. The pattern is:
1. Generate a tiny ISO containing a `user-data` YAML file with `ssh_authorized_keys: [<user's pubkey>]`
2. Attach the ISO as a second disk on the VM
3. cloud-init at boot reads the ISO, applies the keys

Works fine. Costs:
- 5–15s boot overhead (cloud-init has its own service-startup choreography)
- Need cloud-init installed in the rootfs (~50 MB)
- ISO build per VM (small but nonzero)
- An extra virtio-blk drive at runtime

### Solution C — Kernel cmdline injection

The mechanism SmolVM uses. Take the user's pubkey, base64-encode it, append to the kernel cmdline as `smolvm.authorized_key_b64=<base64>`. The init script reads `/proc/cmdline`, finds the param, decodes, writes it.

```sh
# inside /init
AUTHKEY_B64=$(cat /proc/cmdline | tr ' ' '\n' \
    | grep '^smolvm\.authorized_key_b64=' | head -1 | cut -d= -f2-)
if [ -n "$AUTHKEY_B64" ]; then
    DECODED=$(echo "$AUTHKEY_B64" | base64 -d 2>/dev/null)
    if [ -n "$DECODED" ]; then
        mkdir -p /root/.ssh
        chmod 700 /root/.ssh
        echo "$DECODED" > /root/.ssh/authorized_keys
        chmod 600 /root/.ssh/authorized_keys
    fi
fi
```

Costs are basically zero:
- One extra arg on the kernel cmdline (a few hundred bytes)
- ~15 lines of shell in /init
- No ISO, no cloud-init, no extra disk

The kernel cmdline has a length limit (~2 KiB on x86, configurable), but a base64-encoded ed25519 public key is ~120 bytes. Plenty of room.

This is why the layered preset rootfs needs `/init` baked in. The plain Ubuntu rootfs from `cloud-images.ubuntu.com` doesn't have a SmolVM init — it has systemd → cloud-init → seed-ISO. Bake our `/init`, set `init=/init` on the cmdline, and we're done. Solution C uniformly across every preset.

---

## Chapter 6: Building a rootfs image

Time to switch from runtime to build time.

You need to produce a file (`rootfs.ext4`) that, when handed to a kernel, looks like a real disk with a bootable Linux on it. Three stages:

### Stage 1 — Get a Linux userspace from somewhere

We use Docker as a build environment, even though Docker has nothing to do with the runtime. The rationale:

- **OCI images already exist** for every Linux distro. Pulling `ubuntu:24.04` gives us a curated, signed, working Ubuntu userspace with `apt` ready to go.
- **`docker build`** is a fine DSL for "install these packages, configure those things." Every dev knows it.
- **`docker export`** dumps the resulting filesystem as a tarball.

For the layered presets:
```dockerfile
# scripts/ci/Dockerfile.base-rootfs (simplified)
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y openssh-server iproute2 curl git python3 nodejs uv
RUN sed -i ... /etc/ssh/sshd_config        # harden sshd
```

`docker build → docker export rootfs.tar` gets us a tarball of the entire userspace.

### Stage 2 — Pack the tarball into an ext4 file

This is the magical part if you've never done it:

```sh
# Create an empty 4 GiB file
truncate -s 4096M rootfs.ext4

# Format it as ext4 (no mounting needed yet)
mkfs.ext4 -F rootfs.ext4

# Mount the file as a filesystem via a loop device
mkdir /mnt/rootfs
mount -o loop rootfs.ext4 /mnt/rootfs

# Extract the tarball into the mount
tar -xf rootfs.tar -C /mnt/rootfs

# Unmount → the file now contains a populated ext4 filesystem
umount /mnt/rootfs
```

What's a "loop device"? It's a Linux kernel mechanism that makes a regular file behave like a block device. You can then `mount` that block device anywhere. The file is the disk.

Loop devices need:
- A Linux kernel (the kernel-level support is `loop.ko` or built-in)
- Permission to call the `losetup` ioctls (effectively, root or `CAP_SYS_ADMIN`)

This is why **Podman on macOS can't build rootfs files inside its containers**. Podman's Linux VM has loop support, but rootless+privileged mode in podman doesn't expose the loop control device to the container. Docker on macOS works because Docker Desktop runs the daemon as root in its VM and exposes things differently. CI's Linux runners (`ubuntu-latest`) have native loop support and run as root, so they always work.

### Stage 3 — Compress for distribution

Raw ext4 is sparse — the file's allocated size is 4 GiB but most is zeroed. zstd at -19 ("max compression") collapses zeros and most ext4 metadata aggressively:

```
codex-amd64-rootfs.ext4         4.0 GB  (sparse on host)
codex-amd64-rootfs.ext4.zst   ~150 MB  (after zstd -19)
```

Our `Stage release assets` step uses `zstd -19 -T0 --rm`:
- `-19` max ratio
- `-T0` use all cores (compression is CPU-bound)
- `--rm` delete the source after success (the runner is ephemeral, no point keeping the 4 GiB version)

Distribution then is a normal HTTP download + zstd decompression in the runtime.

### Layered builds

The interesting bit for SmolVM. Every preset (codex, claude-code, hermes, pi) needs the same base userspace — Node 22, Python 3, uv, git, openssh — plus its own CLI on top. Building each preset from scratch would mean repeating the apt-install + Node-bootstrap dance five times (~5 min × 5).

Instead, the CI workflow has two stages:

1. **Stage 1 — base-rootfs (per arch).** Build the shared base, upload as an inter-job GitHub Actions artifact.
2. **Stage 2 — preset (matrix).** Each preset job downloads the base ext4, mounts it, chroots in, installs only its preset-specific bits, unmounts, compresses, uploads.

The chroot is the important word here. Once you've mounted the base ext4 at `/mnt/rootfs`, you can `chroot /mnt/rootfs` and it looks to processes as if `/mnt/rootfs` is `/`. Any package install inside the chroot writes into the ext4 file. Unmount and the file is now your layered rootfs.

The dance for chroot to work right:
```sh
mount --bind /proc  /mnt/rootfs/proc        # /proc must exist for many tools
mount --bind /sys   /mnt/rootfs/sys
mount --bind /dev   /mnt/rootfs/dev
cp /etc/resolv.conf /mnt/rootfs/etc/resolv.conf  # so DNS works in the chroot
chroot /mnt/rootfs /bin/bash -c 'npm install -g @openai/codex'
umount /mnt/rootfs/proc /mnt/rootfs/sys /mnt/rootfs/dev
```

One subtlety: that `cp /etc/resolv.conf` step copies the **CI runner's** resolv.conf into the rootfs. If you forget to undo it before unmounting, GitHub's runner DNS gets baked into the published rootfs and ends up on every guest VM. SmolVM's `build-preset.sh` saves the original (if any) before overwriting and restores it before unmount. (See PR #271's CodeRabbit fix.)

---

## Chapter 7: Compression and the size game

After Stage 2's chroot install, the rootfs has:
- Base layer (~510 MB)
- Preset layer (~200 MB → 1 GB depending on preset)

Most of that compresses well, but a fresh apt-built rootfs has a lot of stuff that *we* don't need at runtime. Aggressive cleanup at the right point shrinks both the on-disk size and the wire size.

The cleanups that paid off, in rough order of impact:

| What | Where | Why it's there | Saved |
|---|---|---|---|
| `/usr/include/*` | apt's c-dev headers | Pulled in by some lib's depgraph | ~65 MB |
| `/usr/share/{doc,man,info,...}` | apt's docs | Default `apt` behavior; `--no-install-recommends` doesn't strip these | ~5–20 MB |
| `/var/cache/apt/archives/*.deb` | apt-installer cache | Kept by default for reinstall | ~50–100 MB |
| `__pycache__` + `*.pyc` | Python | Regenerated on first import | ~5 MB |
| Extra Node `@node-llama-cpp` backends (CUDA, Vulkan) | openclaw npm tree | npm pulls all platform binaries | ~600 MB on amd64 |
| `~/.cache/uv` (after `uv pip install`) | uv's wheel cache | Speeds up future installs we won't do | ~2 GB on hermes |
| `~/.npm` (after `npm install -g`) | npm's package cache | Same idea | ~350-700 MB |

The general pattern: any tool that has a `--no-cache` or `clean cache` mode, run it. Then `rm -rf` whatever it missed.

Single-layer Dockerfile RUN matters. Docker layer caching is irrelevant for rootfs production (we throw away the Docker image after `docker export`), but if you put cleanup in a *separate* RUN line, the previous layer's data stays in the exported tarball — Docker layers are diffs, not snapshots, but `docker export` flattens them. Keeping the apt install + apt clean in one RUN line means the cleanup actually shrinks the tarball.

```dockerfile
# Wrong: cleanup in a separate RUN doesn't shrink the export
RUN apt-get install -y curl
RUN apt-get clean

# Right: same RUN, cleanup before the layer commits
RUN apt-get install -y curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*
```

Two cleanups that subtly mattered:

**`uv install.sh --no-modify-path`** — uv's installer appends `. "$HOME/.local/bin/env"` to `~/.bashrc` and `~/.profile`. We then `rm -rf /root/.local` after copying uv to `/usr/local/bin`. The source line dangles; on every shell login the user gets `bash: /root/.local/bin/env: No such file or directory`. `--no-modify-path` skips the rc append entirely.

**`touch /var/log/lastlog`** — PAM's `pam_lastlog` module wants to update a binary log of last logins for each user. If the file doesn't exist, it logs `Couldn't stat /var/log/lastlog: No such file or directory` to syslog on every login. systemd-tmpfiles normally creates it; without systemd, we need to create it ourselves at build time.

Tiny things, but they compound. The base rootfs went 543 MB → 487 MB → 405 MB across two cleanup passes.

---

## Chapter 8: SHA pinning and the `--clobber` trap

Once you have an artifact published, you need a way for the runtime to decide: "is this the right one?" The answer is SHA-256.

The pattern:
- The release page hosts `<preset>-<arch>-rootfs.ext4.zst` as a binary asset
- The code has a hardcoded `_<PRESET>_<ARCH>_ROOTFS_SHA = "..."` constant
- At download time, hash the bytes; if the hash doesn't match, error out

This catches:
- A corrupted download (network truncation, etc.)
- A tampered artifact (someone replaced bytes on the release page)
- A drift bug we'll get to in a moment

### Why two version concepts cause drift

SmolVM has two version concepts:
- `__version__` from `pyproject.toml` — the **CLI version** users install via pip
- `IMAGES_RELEASE_TAG` in `published.py` — the **GitHub release tag** the runtime fetches images from

These are intentionally separate. CLI bumps happen for any user-facing change (bug fix, new flag). Image republishes only happen when the actual rootfs/kernel bytes change. If you couple them, every CLI patch triggers an image rebuild — wasteful and slow.

But the decoupling needs to be explicit. The previous `_MANIFEST_VERSION` constant was a string version like `"0.0.14a0"` that *looked* like it should track `__version__`. When the CLI bumped to `0.0.14a1` and `_MANIFEST_VERSION` stayed at `0.0.14a0`, the CI workflows derived the upload tag from `pyproject` (`images-v0.0.14a1`) while the runtime looked for `images-v0.0.14a0`. Silent drift.

The fix was to rename the constant to `IMAGES_RELEASE_TAG`, change its value from a version fragment to a full tag string, and have CI workflows read it from the source file (not derive from pyproject). Image/rootfs tags now use CalVer (calendar versioning), for example `images-2026.06.12.0`, because they are content snapshots with their own cadence. Now there is exactly one place to bump the image release tag, and it's named to make clear it's not the CLI version.

### The `--clobber` trap

`gh release upload` has a `--clobber` flag that overwrites existing assets with the same name. Convenient for re-running CI workflows. **Disastrous for SHA pinning.**

Imagine:
1. You publish `codex-amd64-rootfs.ext4.zst` with SHA `abc123…`
2. You hardcode `_CODEX_AMD64_ROOTFS_SHA = "abc123..."` in the runtime
3. Months later, you re-trigger CI for a different preset; it rebuilds codex too, gets new bytes (`def456…`), uploads with `--clobber`
4. The runtime now downloads `def456…` bytes, computes hash, gets `def456…`, compares to recorded `abc123…`, errors

Bytes silently drifted. Users hit `SHA-256 mismatch` until you re-pull SHAs and update the manifest.

We hit this exactly twice during the v0.0.14 work — once for the kernel, once for openclaw's rootfs. Both times the fix was the same: re-download the live bytes, compute fresh SHAs, update the constants.

Two ways to prevent it:

1. **Drop `--clobber`.** Re-runs against an existing tag fail at upload. Forces explicit `IMAGES_RELEASE_TAG` bumps for any rebuild. We tried this and reverted because the unblock workflow needed re-runs.

2. **Smoke test for drift.** A workflow that fetches every URL, hashes it, asserts against the recorded constants. Runs on every PR + nightly. Catches drift within hours instead of "when a user files a bug."

Option 2 is the planned solution (issue #265).

---

## Chapter 9: How `smolvm <preset> start` actually boots

Time to put the pieces together. What happens when you type:

```sh
smolvm codex start
```

### Step 1 — CLI dispatch

`smolvm` is a Python package. `smolvm codex start` resolves to `cli/main.py::_run_start(args)`. It:

1. Looks up the preset definition (`presets/codex.py`)
2. Resolves host architecture (`amd64` on x86, `arm64` on Apple silicon)
3. Resolves the VMM (`firecracker` on Linux, `qemu` on macOS)
4. Asks: is there a published image for `(codex, amd64, qemu)`?

If yes → published-image fast path. If no → install-at-boot path (boot a generic Ubuntu VM, then run codex's install script over SSH).

### Step 2 — Fetch the image

The published-image fast path:

```python
# pseudocode
entry = MANIFEST[("codex", "amd64", "qemu")]
# → kernel_url: ...vmlinux-amd64.image,  kernel_sha256: ...
# → rootfs_url: ...codex-amd64-rootfs.ext4.zst,  rootfs_sha256: ...

kernel_path = ImageManager.download(entry.kernel_url, entry.kernel_sha256)
rootfs_zst  = ImageManager.download(entry.rootfs_url, entry.rootfs_sha256)
rootfs_path = decompress_zstd(rootfs_zst, alongside=True)
```

`ImageManager.download` is the choke point that:
- Skips download if the cache already has the right SHA
- Streams from the URL, hashing as it goes, to a temp file
- On success, atomic-renames into the cache; on hash mismatch, deletes the temp file and raises

### Step 3 — Build the boot args

```python
boot_args = "console=ttyAMA0 reboot=k panic=1 init=/init"
boot_args += f" smolvm.authorized_key_b64={base64(user_pubkey)}"
boot_args += f" ip={guest_ip}::{gateway}::{netmask}::eth0:off"
```

A real example:
```
console=ttyAMA0 reboot=k panic=1 init=/init
smolvm.authorized_key_b64=c3NoLWVkMjU1MTkgQUFBQUMzTnphQzFsWkRJMU5USTVB...
ip=10.0.2.15::10.0.2.2::255.255.255.0::eth0:off
```

### Step 4 — Start the VMM

QEMU on macOS, Firecracker on Linux. Both end up doing similar things:

```sh
# QEMU equivalent (simplified)
qemu-system-aarch64 \
    -machine virt,accel=hvf \
    -cpu host -smp 2 -m 2048 \
    -kernel  vmlinux-arm64.image \
    -append  "console=ttyAMA0 reboot=k panic=1 init=/init smolvm.authorized_key_b64=..." \
    -drive   file=codex-arm64-rootfs.ext4,format=raw,if=virtio \
    -netdev  user,id=net0,dns=10.0.2.3,hostfwd=tcp:127.0.0.1:2200-:22 \
    -device  virtio-net-pci,netdev=net0
```

Two SmolVM details bundled into that:
- `hostfwd=tcp:127.0.0.1:2200-:22` — port-forward host port 2200 to guest port 22, so `ssh root@127.0.0.1 -p 2200` reaches the guest's sshd.
- `dns=10.0.2.3` — explicitly configure the SLIRP DNS forwarder address in the DHCP advertisement.

### Step 5 — Inside the guest

The kernel loads from `-kernel`, mounts the rootfs from the virtio drive at `/`, and `exec`s `/init`.

`/init` does its six steps from Chapter 3, ending with `/usr/sbin/sshd -e` running and authorized_keys populated.

### Step 6 — Wait for sshd, return

Back on the host, `cli/main.py` polls TCP port 2200 with a 30-second timeout. As soon as the connection succeeds, the CLI prints:

```
╭───── Sandbox Ready ─────╮
│ Started 'codex-davinci' │
│ with codex preinstalled │
╰─────────────────────────╯
Next: smolvm ssh codex-davinci
```

End-to-end, on a warm cache, this takes 5–10 seconds. On a cold cache (first download), add ~30s for the rootfs zstd download.

---

## Chapter 10: Things that broke, and what they taught us

A few war stories from the v0.0.14 rollout, with the take-aways generalized.

### Stale SHAs after a `--clobber` re-upload

Symptom: `SHA-256 mismatch for codex-amd64-rootfs.ext4.zst`.

Lesson: hardcoded SHAs in source require a discipline to re-pull whenever bytes change. CI smoke testing catches it before users do. Naming matters — calling the constant `_MANIFEST_VERSION` invited the assumption that it tracked `__version__`, which produced the drift. `IMAGES_RELEASE_TAG` is harder to misuse.

### `paramiko` ImportError in the kernel build job

Symptom: kernel build CI tried to read `IMAGES_RELEASE_TAG` by importing the package, which chained through `smolvm/__init__.py` → `smolvm.browser` → ... → `paramiko`, which the kernel job didn't install.

Lesson: don't import a Python package just to read a string constant. `awk -F'"' '/^IMAGES_RELEASE_TAG[[:space:]]*=/{print $2; exit}' src/smolvm/images/published.py` is faster and has no dependency surface.

### `zstd --rm` permission denied

Symptom: `build-preset.sh` runs under `sudo` (mount + chroot need root), so the output ext4 is root-owned. The next CI step's `zstd --rm` runs as the runner user and can't delete the source.

Lesson: when sudo'd processes write files that subsequent unprivileged steps need to manipulate, chown back. `sudo chown -R "$(id -u):$(id -g)" /tmp/out` after the sudo'd step.

### `init=/init` against a rootfs that has no `/init`

Symptom: kernel boots, mounts rootfs, finds no `/init`, falls back to `/sbin/init`. Sometimes that works (Ubuntu has systemd there). Sometimes it doesn't. Either way, our `smolvm.authorized_key_b64=...` cmdline param goes unread because nothing parses it.

Lesson: a kernel cmdline mechanism that depends on a userspace cooperator only works if the cooperator is actually present. Bake `/init` into the rootfs at build time, don't assume it's there.

### Hardcoded `debian-bookworm` OS label

Symptom: `smolvm codex start` reported `OS: debian-bookworm`. But codex runs on Ubuntu 24.04 in our layered build.

Lesson: when you generalize a code path that used to be single-purpose, audit the per-purpose constants too. `debian-bookworm` was correct when only openclaw existed; it was wrong as soon as four more presets joined. A `_PRESET_OS_LABEL` lookup table is more maintenance but makes the per-preset variation explicit.

---

## Chapter 11: Where the build pipeline goes from here

A few open threads, in the order I'd tackle them.

### `smolvm create` uses our base rootfs (#273)

`smolvm create` now uses the published Ubuntu base rootfs from the SmolVM image release instead of downloading Ubuntu's official cloud image from `cloud-images.ubuntu.com`. That removes the last external CDN from the boot path and keeps the default image pinned by SHA.

The remaining work is operational: keep the base ext4 published alongside preset images, keep `/init` in the base Dockerfile, and bump the image release tag when those bytes change.

### Drift smoke test (#265)

A nightly + on-PR workflow that walks `BASE_KERNELS` + `MANIFEST`, fetches each URL, hashes, asserts. Fails loudly if any live byte drifts from a recorded SHA. Closes the `--clobber` class of bug for good.

### Move openclaw to layered base

Right now openclaw uses its own `build_openclaw_rootfs` Docker pipeline. Predates the layered pattern. Migrating it to the same `Dockerfile.base-rootfs` + `build-preset.sh` flow drops ~250 LOC and gives every preset the same userspace.

The risk is openclaw's sidecars (device-approver, systemctl proxy) — they're load-bearing for production use. Need careful boot testing before flipping over.

### Init script parity (#275)

Both inits (preset + openclaw) hardcode `/24` and Google DNS. Real bug for non-/24 networks. Fix touches both files identically — a half-day at most.

### Alpine flavor (#264)

Alpine 3.20 with musl libc would shave another ~150–200 MB off the base. Risky because some npm/Python packages ship glibc-only prebuilts; pure-JS presets (codex, claude-code, pi) are likely fine, hermes (with `[all]` extras) and openclaw (with `@node-llama-cpp` natives) are not. Roll out preset-by-preset.

---

## Appendix: Reading order if you actually use this codebase

If you're new and want to internalize how the image pipeline works:

1. Read [`scripts/ci/Dockerfile.base-rootfs`](../../scripts/ci/Dockerfile.base-rootfs) — 60 lines, gets you the base userspace
2. Read [`scripts/ci/build-base-rootfs.sh`](../../scripts/ci/build-base-rootfs.sh) — 40 lines, Docker → tar → ext4
3. Read [`scripts/ci/preset-init.sh`](../../scripts/ci/preset-init.sh) — 90 lines, the actual /init that boots
4. Read [`scripts/ci/build-preset.sh`](../../scripts/ci/build-preset.sh) — 130 lines, layered build via chroot
5. Read [`src/smolvm/images/published.py`](../../src/smolvm/images/published.py) — manifest, SHA pinning
6. Read [`src/smolvm/cli/main.py::_run_start_with_published_image`](../../src/smolvm/cli/main.py) — runtime dispatch
7. Read [`.github/workflows/build-published-images.yml`](../../.github/workflows/build-published-images.yml) — CI orchestration

The whole image pipeline is about 600 lines of code. The hard parts are the chapter headings above, not the line count.
