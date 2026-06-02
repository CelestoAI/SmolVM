# SmolVM boot-latency learnings (QEMU vs Firecracker)

**Takeaway:** out of the box, Firecracker gets you to your first command faster
than QEMU — but only because QEMU's default was hitting an 8-second bug (now
fixed). Once that's removed, both reach a usable sandbox in ~1.2–1.4 seconds,
and almost all of that time is the guest operating system starting up, not the
choice of virtual-machine engine. That matters because it tells you where to
spend effort to make sandboxes start faster: in the guest, not the hypervisor.

## Test setup

Numbers measured on one Linux host with KVM, 16 CPUs, an Alpine Linux guest
(1 CPU / 512 MB), running `echo hello`. Scripts live under `scripts/`:
`bench_backends.py`, `profile_boot.py`, `exp_vsock_trim.py`, `exp_userspace.py`.

> **Methodology note.** SmolVM's `.start()` returns as soon as the hypervisor
> *process* is up — it does **not** wait for the guest to finish booting. The
> guest boot (kernel + init + sshd/agent) happens during the *first command*,
> while the SDK waits for the control channel. So the only honest headline
> metric is **TOTAL → interact** = create + launch + first-command (wall-clock
> from nothing to a command returning). Splitting the phases flatters whichever
> backend defers more work.

## Headline numbers

| Configuration | create | launch | first cmd | **TOTAL→interact** | warm cmd |
|---|---:|---:|---:|---:|---:|
| Firecracker, SSH (default image) | 135 | 121 | 1337 | **1594 ms** | 43 |
| QEMU, SSH (default image) | 9 | 53 | 1879 | **1940 ms** | 42 |
| QEMU, vsock (default boot) | 11 | 53 | 1507 | **1571 ms** | 1 |
| QEMU, vsock + trimmed boot | 11 | 54 | 1278 | **1343 ms** | 1 |

All in milliseconds. Variance was tiny (stdev ≤ 2 ms per phase, except
Firecracker's first-command at ±72 ms).

## Finding 1 — Firecracker is faster to a usable VM, not slower

Out of the box (each backend's default guest), Firecracker reaches an
interactive command in **~1.59 s vs QEMU's ~1.94 s** — ~350 ms (18%) quicker,
and that is on Firecracker's *unprivileged* network path (it prints "Using the
slower networking path… Run with sudo to use the faster path."). With `sudo`
its create/launch overhead would shrink further.

An earlier draft wrongly reported QEMU as faster — that was an artifact of
quoting the create+launch sub-phases (tens of ms) instead of the end-to-end
total. The hypervisor's own overhead is <10% of the wall-clock; **~95% is the
guest booting.**

## Finding 2 — the bottleneck is guest boot, dominated by userspace

Breaking the ~1.5 s first-command into real sub-steps (`profile_boot.py`):

- **create + launch**: 60–260 ms (hypervisor only).
- **guest kernel boot**: ~1.0–1.2 s of in-kernel time (last printk).
- **userspace init** (host-key generation + sshd/agent startup): the rest.
- **SSH handshake + auth**: ~68 ms.
- **command exec**: 3–43 ms.

QEMU's guest takes ~250 ms longer to reach userspace than Firecracker's because
it emulates more devices the kernel must probe (biggest single stall:
`ata1: SATA link down`, ~260 ms; plus ACPI, PS/2, VGA). Firecracker's lean
virtio-only model (`pci=off`, no SATA/VGA) avoids most of it — which is the
whole point of a microVM.

## Finding 3 — vsock helps, but is QEMU-only and needs python3 in the image

`comm_channel="vsock"` uses a persistent guest agent instead of SSH.

- **first command**: ~370 ms faster (1879 → 1507) — skips the SSH handshake and
  first-boot host-key generation.
- **warm command**: **42 ms → 1 ms** (~40× faster). This is the biggest single
  win for command-heavy workloads: SSH pays a fresh exec round-trip per command;
  the vsock agent is a kept-open connection.

Two real blockers found:

1. **QEMU-only today.** `comm/select.py` gates vsock to the QEMU backend;
   requesting it on Firecracker raises. QEMU uses native `AF_VSOCK` over the
   host's `/dev/vhost-vsock`; Firecracker multiplexes vsock over a host Unix
   socket whose host-side bridge client SmolVM hasn't implemented.
2. **Default image can't run the agent.** The auto-config image
   (`ImageBuilder.build_alpine_ssh_key`) installs `openssh iproute2 curl bash`
   but **no `python3`**. The guest agent is a Python script that `/init` only
   launches `if command -v python3`. No python3 → agent never starts → vsock
   times out and (in auto mode) silently falls back to SSH. The password-auth
   recipe (`build_alpine_ssh`) *does* install python3, which is what made vsock
   measurable here. **This looks like a latent bug worth filing:** the default
   image bakes the agent binary but omits its runtime.

Host requirement (present on this machine): `vhost_vsock` module loaded and
`/dev/vhost-vsock` available.

## Finding 4 — boot-cmdline trimming: real but smaller than it first looked

Adding `acpi=off quiet no_timer_check tsc=reliable` dropped the kernel's
*last printk* from ~1.15 s to ~0.41 s — but **most of that is a measurement
artifact**: `quiet` suppresses late printks, so "last printk" understates true
boot time. The ground-truth `SMOLVM_TS` uptime markers baked into `/init` show
userspace actually starting at ~0.87 s (trimmed) vs ~0.94 s (default) — a real
saving of **~70 ms**, mostly from `acpi=off` skipping ACPI/device probing.
Total-to-interact still improved (1571 → 1343, ~230 ms) because `quiet` also
cuts time spent formatting console output over the serial line.

Caveat: `acpi=off` is safe for this minimal headless virtio guest but not
universally (some setups need ACPI for clean shutdown / IRQ routing); validate
per image before defaulting it.

## Userspace timeline (ground truth, from `SMOLVM_TS` markers)

Default boot, SSH path:

```
0.94s  kernel boot done / mounts-ready
0.95s  net-ready            (+10ms)
0.96s  guest-agent-started  (+10ms)   <- vsock becomes answerable here
0.96s  ssh-hostkey-check-start
1.15s  ssh-hostkey-check-done (+190ms) <- ssh-keygen -A, runs EVERY boot
1.16s  sshd-invoked         (+10ms)    <- SSH becomes answerable here
```

Key insight: the Dockerfile deletes host keys (`rm -f /etc/ssh/ssh_host_*`) so
`/init` regenerates them on **every** boot — a fixed **~120–190 ms** tax on the
SSH critical path. vsock skips it entirely (agent is ready at 0.96 s, before
keygen). This is the lever Experiment C targets.

## Finding 5 (Experiment C) — the SSH bottleneck is host-side, not the guest

Baking SSH host keys at build time (so `/init` skips `ssh-keygen -A`):

| Cell | keygen in guest | guest sshd ready | **TOTAL→interact** |
|---|---:|---:|---:|
| C0 baseline (keygen each boot) | 123 ms | 1.10 s uptime | **1941 ms** |
| C1 baked host keys | 8 ms | 1.00 s uptime | **1940 ms** |

Baking keys made the **guest** ready ~100 ms sooner — but **total time-to-interact
did not move (1941 → 1940 ms).** The guest is not the bottleneck on the SSH path.

A tight-polling probe (10 ms) showed real SSH auth succeeds at **~1601 ms** after
launch, while the SDK reports **~1878 ms**. So on the SSH path the wall-clock is
set by the **host-side wait loop**, not guest readiness:

- The SDK's `SSHClient.wait_for_ssh` Phase-2 connect loop polls every **200 ms**
  (`time.sleep(min(0.2, remaining))`), adding up to ~280 ms of pure slack.
- QEMU's user-mode NAT forwarder accepts the host's TCP connection *immediately*
  (before guest sshd is really up), so the fast TCP probe returns instantly and
  the real wait is hidden in repeated paramiko handshake retries against a
  not-yet-ready sshd.

This is exactly why **vsock wins**: it bypasses the SSH wait loop entirely and
becomes answerable the moment the agent starts (~0.9 s uptime). Guest-side
userspace trims (baked keys) only pay off once the host-side wait is also tight
or replaced by vsock.

## Finding 6 — default QEMU auto-config has an 8-second vsock-probe penalty

The most severe issue found. On the QEMU backend, the channel selector
*auto-prefers* vsock with SSH fallback (`vsock_possible = is_qemu and …`). But
the default auto-config image lacks python3, so the agent never answers. The
host then waits the full `_VSOCK_AUTO_PROBE_TIMEOUT = 8.0 s` before falling back
to SSH:

```
QEMU auto-config (default), first command: 8066 ms   <- 8s vsock probe + SSH
QEMU explicit comm_channel="ssh", first command: 1876 ms
```

So the real out-of-box QEMU number is **~8.1 s to interact**, not ~1.9 s — the
earlier ~1.9 s figures were taken with vsock-capable or explicit-SSH paths.
Two independent bugs compound here:

1. Default image bakes the agent but omits its python3 runtime (Finding 3).
2. Auto-mode pays an 8 s timeout discovering that, every single boot.

Mitigations (any one fixes it): ship python3 in the default image; or shorten
the auto vsock probe; or have auto-mode skip vsock when the image has no agent;
or default QEMU auto-config to `comm_channel="ssh"`.
```
