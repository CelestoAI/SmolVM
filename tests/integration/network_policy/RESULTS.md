# Network policy validation

These tests check that sandboxes can reach only approved website names. Public
startup and restore remain gated; this is not a released feature.

| Suite | Latest result | What it proves |
| --- | --- | --- |
| SDK/facade | 453 passed, 8 skipped | Configuration, lifecycle ordering, cleanup, cached-channel checks; mocked runtime boundaries |
| Wire | 79 passed, 42 dependency warnings | Real HTTP/TLS sockets; controlled resolver/socket substitutions in destination cases |
| Linux | 44 passed | Real TAP packets, nftables, worker isolation, persistent supervisor and verified shutdown; sleeping child stands in for VM |
| QEMU | 5 passed | Two ARM64 TCG guests, real proxy/firewall; `init=/bin/bash`, not the native guest agent |

The wire cases cover authority/SNI conflicts, same-IP names, unsafe destinations,
upstream verification, malformed headers, unsupported protocols, streaming/SSE,
configuration drift and bounded concurrent admission. Linux cases cover spoofing,
cross-listener access, direct egress, worker credentials, expiring admission,
rule drift, supervisor death and orphan recovery. QEMU cases cover hostile-root
traffic, proxy crash/restart, public trust rotation and a copied stopped disk.
These are regression tests, not exhaustive security or capacity evidence.

### Native GCP validation — 2026-09-08

Native lifecycle smoke passed on a disposable GCP `n1-standard-1` Spot VM with nested KVM, Ubuntu 24.04, Linux `7.0.0-1011-gcp`, Python 3.12.3 and QEMU 8.2.2. The harness bypassed **only the release gate**, using the actual manager, paused QEMU launch, persistent supervisor, guest `/init`, vsock agent, public trust installation and cleanup. No transport, firewall, resolver or engine substitutions.

- Deny-all and `example.com` allowlist: boot, real vsock exec, direct-IP/metadata denial, stop, restart, pause/resume and verified cleanup passed.
- Allowed HTTPS succeeded; `example.net` was denied. A worker SIGKILL killed its QEMU guest and cleanup succeeded.
- The creating SDK process exited; after four seconds a new facade adopted the live VM and successfully ran HTTPS. A cached facade rejected command dispatch after explicit fencing.
- Single post-request sample: worker RSS 80,448 KiB / 7 open FDs; supervisor 34,088 KiB / 7 FDs; QEMU 167,640 KiB / 34 FDs. This is not sustained load or capacity evidence.
- The VM and auto-delete boot disk were deleted after evidence collection. An initial e2-small lacked KVM; the first nested-KVM Spot was preempted during setup. No existing cloud resources were changed.

**Published-image blocker:** the current `images-2026.09.07.0/pi-amd64-alpine-rootfs.ext4.zst` matched pinned SHA `ceffda243035829895034cc7db7ec254f27dbe683a30343d11778b64c7389023`, but boot panicked with `/init` error -8. A separate filesystem copy, after journal recovery, had zero-byte `/init` and `/usr/local/bin/smolvm-guest-agent`. The original downloaded image was not repaired or substituted in-place.

The passing run used the unmodified older `images-2026.06.30.0` Pi Alpine image, SHA `663411193e9792f3cb5fce88a1b55f7ca12064021d58b4d5b0ed274d2169c460`, with the manifest-pinned amd64 kernel SHA `a7a8da6ad55236edccbdd1015eda023d68a4879be648a348462119138de54cb3`. This validates native policy wiring on that image, **not the current image release**. Public start and restore remain gated. The local macOS Docker kernel still lacks vsock; no workaround was added.

## Remaining release gates

- Repair and validate the current published image, including native guest trust
  and client compatibility. Restore needs fresh trust and remains gated.
- Complete host-service privilege/setup integration and independent review.
- Run bounded sustained resource/failure checks and a representative customer
  HTTP/1.1 workload. Single RSS samples and synthetic timings are not capacity
  or customer compatibility evidence.
- Wire Celesto persistence/placement acknowledgements only after OSS review;
  no cloud API or image rollout is enabled by these tests.

See [reproduction instructions](README.md). Native scripts were preserved from
that GCP run with explicit opt-in and image pins; their reorganized copies have
not yet been rerun on GCP.
