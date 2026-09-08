# Network-policy enforcement spike

This experiment checks whether a sandbox can restrict HTTP requests to approved names, even when its root client lies about the destination. It is **not a shipped feature or a safe standalone proxy**. Keep it inside the disposable test environment; never install these fixture rules on a compute host.

## Verified evidence

| Suite | Result | Scope |
| --- | --- | --- |
| Engine | **79 passed** on macOS/Python 3.12 and 3.13, and Linux ARM64/Python 3.13 | Real sockets and TLS; controlled resolver/socket substitutions in destination cases. |
| Linux | **44 passed** | 29 TAP/nftables cases, 6 worker cases, and 9 persistent-supervisor/native-shutdown cases, without QEMU. |
| Combined QEMU | **5 passed** | Hostile root guest, separate origin guest, real managed proxy, host resolution and nftables. |

The implementation now lives in `src/smolvm/network_policy/` on branch `feat/strict-network-policy`: canonical policy, engine, per-VM firewall, worker owner, prerequisite checks and public trust/environment handoff. This harness imports/copies those modules, not a separate engine. **Native lifecycle integration is in progress behind a start/restore gate; cloud APIs are not wired.** No public feature, migration or deployment has been enabled. All traffic and credentials are synthetic.

An additional **94 focused SDK tests** cover the contract, setup, guest handoff, firewall rendering and legacy policy; **145 existing network/VM/setup regressions pass**. Guest handoff tests use a real public certificate and a fake control channel; they do not prove production init/exec/terminal configuration.

### Engine

- Allowed HTTP and inspected HTTPS reach an origin using the engine's actual parsers and TLS implementation.
- Disallowed/mismatched authorities, duplicate/missing Host, selected folded/control-containing headers, unsafe Connection tokens and upgrades are rejected before dialing.
- CONNECT, SNI and every inner Host agree. Inner absolute-form requests are rejected because the engine rewrites some target fields before the hook; v1 supports origin-form HTTPS requests instead.
- The numeric dial address retains approved upstream SNI and certificate verification. Wrong-host and untrusted origin certificates fail.
- Destination validation rejects the tested private, metadata, loopback, multicast, shared-address, documentation, reserved and globally reachable special-purpose addresses, supplied public host addresses, mixed/empty answers, IPv6 and noncanonical numeric strings.
- Controlled DNS/socket tests observe one resolution followed by a validated numeric dial. Rebinding to metadata blocks the next connection before its socket seam. These substitutions are **not** packet-level proof for every destination.
- Default-deny guards cover client admission, protocol selection, CONNECT, TLS, request/response headers and server connection. Injected validation, TLS, resolution and protocol-selection failures leave denial in place.
- Required addon inventory, relevant hook bindings, effective options and domain/host-address intent are sealed before running. Mutating TLS verification, passthrough selectors, the allowlist or required guard inventory prevents further requests. There is no dynamic script loader, stock addon bundle or administrative interface.
- Each connection carries **one HTTP exchange**, with `Connection: close` on both sides. Subsequent pipelined requests are not forwarded, even to an allowed name. Both sockets are released after completion even if the origin ignores closure.
- SSE arrives before origin closure. HTTP/HTTPS upload chunks stream before completion, with an exact final digest for a body over 512 KiB; request-looking bytes remain payload.
- A provisional 32-client admission cap rejects the next client before HTTP/TLS parsing and releases capacity on disconnect. This is not an accept-flood/DoS guarantee or a measured production limit.

Tests reproduced failures before fixes: folded/control-containing headers reaching origins; a missing request hook inheriting earlier permission; unexpected 101 forwarding; `is_global` admitting multicast/special-purpose addresses; and runtime TLS-verification/configuration changes remaining effective. These are candidate test observations, **not exploits in a deployed Celesto policy**.

### Linux firewall and worker supervision

The 29 firewall cases use the runtime rule generator with real TAP frames, kernel sockets and nftables:

- Two source/TAP identities reach only their assigned listener fixtures. Source spoofing, cross-listener access, host services/public host addresses, direct external forwarding, metadata, other guests, direct DNS, UDP and guest IPv6 exercise drops.
- Proxy-identity OUTPUT permits public TCP 80/443 and its trusted resolver, while rejecting tested private/special/local targets, alternate DNS, UDP 443 and other ports. Established replies to gateway readiness probes are distinguished from forbidden new gateway connections.
- Assertions inspect counters **and outgoing packets**, with actual listeners to detect accidental host access. Earlier blanket accepts do not override the policy's drops.
- Each VM owns a separate table. Atomic rebuild closes admission without changing its neighbor; deny-all leaves no admitted proxy. A kernel `fib daddr type local` check blocks newly assigned host IPs even before an inventory refresh.
- An earlier negative control replacing fixture drops with accepts produced **16 failures** in the then-25-case suite. That control has not been rerun for the expanded suite.

The five process cases launch `worker.py` through `managed_proxy.py`:

- Failed worker startup closes its admission without removing another fixture member, and cleans up.
- Kernel `/proc` inspection verifies that a synthetic host credential environment variable and deliberately inheritable descriptor do not cross launch. Worker stderr is `/dev/null`.
- A fencing error still stops the worker, removes its private directory and reports failure. **It does not implement VM quarantine.**
- A live worker cannot be replaced through repeated start. Cleanup is idempotent; closed instances cannot restart.
- Concurrent cleanup and closing an unstarted instance are safe.

The supervisor serializes lifecycle operations separately from firewall operations so joining its crash watcher does not deadlock fencing. Before admission it verifies config identity/PID, actual kernel UID/GID/groups/capabilities/no-new-privileges, and a negative HTTP readiness request. `setpriv` drops privileges before importing the engine, with an allowlisted environment and closed extra descriptors. The worker disables logs/warnings before import, uses umask 077, disables core dumps and applies 256-FD/1-GiB-address-space limits. Only the bounded readiness message and public CA are returned to the parent.

The fixture uses UID 65534. Native placement now derives distinct UIDs and stable ports from NAT leases; end-to-end native validation remains pending. Linux parent capabilities are explicitly limited to NET_ADMIN, SETUID, SETGID, CHOWN, DAC_OVERRIDE, SETPCAP and KILL. Worker capabilities, including its bounding set, are zero.

### Combined root QEMU guest

Two ARM64 QEMU/TCG guests boot SHA-verified published SmolVM images: the hostile guest and a separate HTTPS origin. Both allowed and denied names resolve through the host OS to the origin's same numeric address. No candidate resolver or socket hooks are mocked here. Firewall rules exist before hostile guest boot; proxy admission starts closed.

The five cases establish:

1. Allowed HTTPS works; same-IP disallowed names, conflicting inner Host and direct connections fail from root.
2. Proxy crash closes admission. Restart creates a fresh CA; stale guest trust fails until replaced.
3. The real worker has no capabilities/supplementary groups and has the expected UID, no-new-privileges and limits. Its private CA is worker-owned and inaccessible to a different UID. Only its public CA enters the guest. A credential-bearing malformed request leaves no captured flow/log files or extra stdout; its private directory contains only expected CA/DH material.
4. Curl and a Python urllib HTTPS client work with explicit proxy/trust configuration; initial process and latency measurements are captured.
5. A stopped/synced qcow2 overlay copied into a new guest remains restricted; new public trust is required after proxy replacement.

**Restore scope:** this is a disk-copy/reboot fixture using `init=/bin/bash`, not native SmolVM snapshot restore, the cloud restore constructor, or the production init/guest-agent path. No KVM-backed or production host was contacted. Guest stdlib imports receive a longer fixture deadline under TCG; client network timeouts remain bounded.

The latest runtime-source five-sample run measured **78,276 KiB (~76.4 MiB) worker RSS/peak RSS**, direct median **66.69 ms**, proxied median **70.73 ms**. The origin is itself TCG-emulated. These are initial observations, **not a throughput, saturation, steady-state streaming or production capacity benchmark**. They do not justify a placement reservation yet.

### Persistent ownership and recovery

`PreparedPolicy` starts a detached supervisor before boot, then attaches the VM's actual PID and birth identity through a kernel process handle. A per-placement file lock prevents duplicate owners without changing an existing policy. Public CA, process identities and binding metadata are saved atomically; private key bytes are never in that state file.

The Linux tests verify:
- The SDK client process can exit while the attached supervisor/worker remain active.
- VM exit stops its worker; worker failure or rule drift kills the attached VM through its process handle, not a potentially recycled numeric PID.
- Adoption checks live process identities and an effective-rule fingerprint. Counter and lease-countdown changes do not cause false drift.
- Kernel admission expires after three seconds unless renewed. Unexpected supervisor EOF keeps the worker's listening port occupied for four seconds, beyond admission expiry. Graceful shutdown releases it immediately only after verified fencing.
- Supervisor SIGKILL is recoverable without its cleanup handler: admission expires, then its worker exits. The orphan VM remains fenced until the VM owner stops it. Cleanup refuses live ownership and removes orphan keys, rules and metadata only after verified shutdown; completed cleanup is idempotent.

Before freeing the VM's IP/UID reservation, the runtime owner must verify actual VM shutdown and call `cleanup_stopped_policy`. Missing metadata is not proof that an interrupted VM launch created no process. Native placement and root-service preflight are implemented; cloud host-service integration remains pending.

### Python/setup compatibility

The engine passes on Python 3.12 and 3.13. The policy/lifecycle package imports on Python 3.11 with the base Pydantic dependency and without importing mitmproxy; attempting the worker on 3.11 exits 2 before engine startup, with empty stdout/stderr. Strict support is now declared as optional `smolvm[network-policy]` for Linux/Python 3.12+. The default dependency list and base Python 3.11 requirement remain unchanged. Preflight rejects a missing/unreviewed engine or missing OS tools before launch; deny-all needs no engine or new Python. No private interpreter manager is planned. Users install the optional feature, not a separately configured mitmproxy daemon.

## Native integration progress (gated)

The latest SDK regression run has **453 passing tests and 8 skips** across policy, placement, launch, guest trust, cancellation, shutdown, VM operations, facade operations, QEMU snapshots and legacy internet settings. The separate wire suite has **79 passing tests** (42 dependency warnings). Paused launch attaches the persistent supervisor before guest execution. Guest trust uses the real channel API, but unit tests replace the channel. Reconnect and resume verify live policy ownership; snapshot-error recovery checks again before continuing. Initial QEMU resume checks ownership after its readiness wait. Cached vsock readiness, command execution and file/environment operations recheck policy rather than trusting a previously ready channel. Shutdown uses process birth identity and kernel handles, tolerates exit racing a signal, and retains placement unless death and cleanup are verified. Linux shutdown coverage uses an actual sleeping child, not QEMU.

### Native GCP validation — 2026-09-08

Native lifecycle smoke passed on a disposable GCP `n1-standard-1` Spot VM with nested KVM, Ubuntu 24.04, Linux `7.0.0-1011-gcp`, Python 3.12.3 and QEMU 8.2.2. The harness bypassed **only the release gate**, using the actual manager, paused QEMU launch, persistent supervisor, guest `/init`, vsock agent, public trust installation and cleanup. No transport, firewall, resolver or engine substitutions.

- Deny-all and `example.com` allowlist: boot, real vsock exec, direct-IP/metadata denial, stop, restart, pause/resume and verified cleanup passed.
- Allowed HTTPS succeeded; `example.net` was denied. A worker SIGKILL killed its QEMU guest and cleanup succeeded.
- The creating SDK process exited; after four seconds a new facade adopted the live VM and successfully ran HTTPS. A cached facade rejected command dispatch after explicit fencing.
- Single post-request sample: worker RSS 80,448 KiB / 7 open FDs; supervisor 34,088 KiB / 7 FDs; QEMU 167,640 KiB / 34 FDs. This is not sustained load or capacity evidence.
- The VM and auto-delete boot disk were deleted after evidence collection. An initial e2-small lacked KVM; the first nested-KVM Spot was preempted during setup. No existing cloud resources were changed.

**Published-image blocker:** the current `images-2026.09.07.0/pi-amd64-alpine-rootfs.ext4.zst` matched pinned SHA `ceffda243035829895034cc7db7ec254f27dbe683a30343d11778b64c7389023`, but boot panicked with `/init` error -8. A separate filesystem copy, after journal recovery, had zero-byte `/init` and `/usr/local/bin/smolvm-guest-agent`. The original downloaded image was not repaired or substituted in-place.

The passing run used the unmodified older `images-2026.06.30.0` Pi Alpine image, SHA `663411193e9792f3cb5fce88a1b55f7ca12064021d58b4d5b0ed274d2169c460`, with the manifest-pinned amd64 kernel SHA `a7a8da6ad55236edccbdd1015eda023d68a4879be648a348462119138de54cb3`. This validates native policy wiring on that image, **not the current image release**. Public start and restore remain gated. The local macOS Docker kernel still lacks vsock; no workaround was added.

## Remaining gates

- Bounded load/failure checks for the supported contract; expanded protocol exploration and optimization are deferred.
- Finish host privilege/setup integration for the persistent VM supervisor. The optional dependency/interpreter strategy is settled; do not reopen engine or interpreter-manager research.
- Repeat native placement, paused launch, attachment, guest trust, adoption and shutdown on a repaired current published image. Restore remains gated and needs fresh-trust handling. Reserve identities until shutdown/cleanup is verified; avoid listener reuse while a failed placement still exists.
- Real guest init/exec/terminal trust configuration and client matrix. Existing long-lived SSL contexts may need recreation after CA rotation; overwriting a certificate file alone does not prove recovery.
- Sustained memory/FD/CPU/stream budgets, privacy under all failure paths, mixed-VM saturation and consistent cloud resource reservations. Suppressed worker diagnostics must eventually gain bounded, non-sensitive reason codes.
- A requesting customer's representative HTTP/1.1 workload. Synthetic curl/urllib tests are not customer compatibility evidence.
- Independent review and baked-image testing before cloud exposure.

**Not feature-complete.** The original combined QEMU harness uses `ManagedProxy` directly; the separate GCP run now exercises native `PreparedPolicy`, stopped-policy cleanup, allocation and real guest configuration. Repair and validate the current image release and complete the remaining gates before wiring Celesto. Do not expose the public field until these paths work. Expanded protocol research and optimization are deferred in favor of shipping the narrow supported feature.

## Reproduce

From this directory:

```sh
uv sync --frozen --python 3.13
uv run pytest -q --disable-warnings
```

For the Python 3.12 floor without replacing the existing virtual environment:

```sh
UV_PROJECT_ENVIRONMENT=/tmp/smolvm-policy-py312 uv run --frozen --python 3.12 pytest -q --disable-warnings
```

For the unprivileged, network-disabled engine suite:

```sh
docker build --build-context policy=../../../src/smolvm/network_policy -t smolvm-network-policy-spike .
docker run --rm --network none --cap-drop ALL \
  --security-opt no-new-privileges --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=256m --memory 512m --cpus 2 \
  smolvm-network-policy-spike
```

For the separate kernel/process and combined guest suites:

```sh
./run-linux-tests.sh
./run-qemu-tests.sh
```

Use Docker Desktop/Linux with `/dev/net/tun`; **never substitute host networking, host mounts or `--privileged`**. QEMU uses its own network namespace, read-only root, disposable 512-MiB `/tmp`, 2-GiB memory and four CPUs. Build-time downloads are pinned in `Dockerfile.qemu`; `_assets/` contains only ignored public images. Code/test changes reuse the large dependency/image layers.

The default pytest invocation selects only engine tests. Engine runs currently report 42 dependency deprecation warnings; no tests are skipped in the selected suites. Record exit status and retain full output when running asynchronously; interrupting an attached `docker run --rm` can lose the final result.
