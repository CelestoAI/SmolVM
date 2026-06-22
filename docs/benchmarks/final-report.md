# SmolVM boot-latency: before / after report

**Goal:** measure how long it takes to create a SmolVM sandbox and run a command,
find the bottleneck, and improve it.

**Setup:** one Linux host with KVM (16 vCPUs). Guest: Alpine, 1 vCPU / 512 MB.
Workload: `echo hello`. Metric: **TOTAL→interact** = create + launch +
first-command = wall-clock from nothing to a command returning. 5 timed runs per
cell after an untimed warm-up; variance was small. Scripts: `scripts/exp_final.py`
(headline), plus `bench_backends.py`, `profile_boot.py`, `exp_vsock_trim.py`,
`exp_userspace.py`. Full reasoning in `boot-latency-learnings.md`.

This is a historical report. The old Python guest-agent and QEMU-only vsock
limitations described below have since been replaced by the standalone Rust
guest-agent and Firecracker vsock support. Current auto-selected vsock requires
that Rust guest-agent; users who need SSH can force it with `--comm-channel ssh`.

---

## Before / After

Same machine, same 5-run methodology, out-of-box defaults (`SmolVM(backend=...)`
with no forced channel). BEFORE measured on `main`, AFTER on
`perf/boot-latency-fixes`; only the code differs. Image cache busted between
runs so each builds its own image.

| Configuration | create | launch | first cmd | **TOTAL→interact** | warm cmd |
|---|---:|---:|---:|---:|---:|
| **BEFORE** — QEMU default (true out-of-box) | 12 | 54 | 8068 | **8134 ms** | 42.2 |
| **AFTER** — QEMU default (now vsock + trimmed boot) | 11 | 53 | 1114 | **1177 ms** | **1.0** |
| BEFORE — Firecracker default (SSH) | 133 | 122 | 1391 | **1645 ms** | 42.6 |
| AFTER — Firecracker default (SSH) | 145 | 121 | 1142 | **1408 ms** | 42.7 |

All values milliseconds, mean of 5 runs.

At the time, `main` resolved QEMU to vsock but the old Python agent could not
run in the image, so it burned the full 8 s probe then silently fell back to
SSH — hence `warm=42 ms` despite the channel reading vsock. On the branch,
vsock genuinely worked (`warm=1.0 ms`).

> Firecracker ran on its unprivileged "slower networking path" in both runs (no
> passwordless sudo), inflating its create/launch equally on both sides — the
> *delta* is still a fair comparison.

### Headline

- **The QEMU default went from 8134 ms → 1177 ms — 6.9× faster** (−6.96 s). The
  default `SmolVM(backend="qemu")` no longer pays the 8-second vsock probe.
- **Warm commands: 42 ms → 1.0 ms (~42×)** now that vsock is the working
  default — this dominates any multi-command (agentic) workload.
- The SSH path that **Firecracker** still depended on also got faster:
  **1645 ms → 1408 ms (1.17×)**, ~240 ms off first-command from the tightened
  poll (Q4) plus the boot trims (Q3). Its warm command stays ~42 ms because it's
  still on SSH. Current SmolVM also has Firecracker vsock coverage.

### What changed (4 commits)

| # | Change | Effect measured |
|---|---|---|
| Q1 | guest-agent runtime present in auto-config image → vsock agent runs | first cmd 8066 → 1509 ms; warm 42 → 1.0 ms |
| Q2 | vsock auto-probe 8s → 2.5s (guardrail for agent-less images) | agent-less fallback 8066 → 2567 ms |
| Q3 | default safe boot trims (`tsc=reliable no_timer_check quiet`) | ~150–230 ms; `acpi=off` left opt-in |
| Q4 | SSH wait loop 200ms fixed → 20ms exp backoff | SSH first cmd ~1878 → ~1669 ms |

> The "BEFORE QEMU" cell was not a strawman — it is what `SmolVM(backend="qemu")`
> did before this branch, dominated by the 8-second agent-probe bug (Q1/Q2).

---

## Where the time went (and the bottleneck)

Decomposing the ~1.5 s first-command:

1. **Hypervisor (create + launch):** 60–260 ms. Not the bottleneck.
2. **Guest kernel boot:** ~0.9–1.0 s of guest uptime to reach userspace.
3. **Userspace init:** networking (~10 ms), then on the SSH path SSH host-key
   generation (~120 ms) + sshd start.
4. **Host-side control-channel wait:** the real SSH bottleneck — see below.
5. **Command exec:** 3–43 ms.

**The bottleneck is not the hypervisor and not even the guest — for the SSH
channel it is the host-side wait loop.** Experiment C proved this: baking SSH
host keys made the guest ready ~100 ms sooner (keygen 123 → 8 ms) yet
**total time-to-interact did not change** (1941 → 1940 ms). A tight 10 ms probe
showed SSH actually answers at ~1601 ms while the SDK's 200 ms-cadence loop
reports ~1878 ms. vsock wins by **bypassing that loop entirely** — its agent is
answerable at ~0.9 s guest uptime.

---

## What each lever was worth

| Lever | Effect | Caveat |
|---|---|---|
| **vsock instead of SSH** | first cmd −370 ms; warm cmd 42 → 1 ms (~28–40×) | Was QEMU-only and Python-runtime dependent in this run; current images use the standalone Rust agent and Firecracker vsock is covered |
| **Trim boot cmdline** (`acpi=off quiet …`) | ~230 ms off total (~70 ms real boot + console savings) | `acpi=off` not universally safe; validate per image |
| **Bake SSH host keys** | guest ready ~100 ms sooner | **0 ms** end-to-end on SSH — host wait loop hides it |
| (host) tighten SSH poll < 200 ms | ~240 ms off the SSH-path first command | shipped in Q4; the AFTER numbers include this change |

The two levers that actually moved the AFTER number are **vsock** and
**boot trimming**. Baking host keys is only worth it once the host-side wait is
also tightened (or replaced by vsock).

---

## Bugs found during this investigation

1. **8-second vsock-probe penalty on QEMU defaults.** QEMU auto-config
   auto-preferred vsock with SSH fallback, but the default image could not run
   the old Python agent, so every boot waited the full 8 s probe before falling
   back to SSH.
2. **Default image shipped the old agent without its runtime.** The image baked
   a Python guest agent but did not install Python, so the agent could never
   start.
3. **Firecracker could not use vsock** in that release because the selector
   hard-gated vsock to QEMU.

These have since been addressed by the Rust guest-agent bake path, requiring
auto-selected vsock to use that agent, and Firecracker vsock support.

---

## Historical recommendations and current status

1. **Fix the 8 s penalty first.** Done: auto-selected vsock now requires the
   Rust guest agent instead of keeping the old hidden SSH downgrade.
2. **Make vsock work out of the box.** Done for published images without a
   Python runtime dependency.
3. **Adopt the trimmed boot cmdline.** Still image/profile-specific; keep
   validating before broadening defaults.
4. **Tighten the host-side SSH wait loop.** Done for explicit SSH.
5. **Implement the Firecracker vsock host bridge.** Done, with E2E coverage for
   Firecracker SSH and vsock.
