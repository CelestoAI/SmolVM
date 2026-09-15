# Public-egress release gate

SmolVM must not advertise public-only internet access until a real browser guest can reach public sites only through the checked host proxy on both supported network backends. This checklist defines the evidence required to expose that API; the internal contract and browser proxy argument are not, by themselves, a public-mode guarantee.

## Current boundary

The repository contains private building blocks for address classification, connection-time resolution, pinned upstream connections, a per-session HTTP/CONNECT proxy, backend network arguments, and an optional Chromium proxy endpoint. Existing browser sessions omit the endpoint and keep their current networking behavior.

Do not add `mode="public"` to Python or `{ mode: "public" }` to TypeScript until every gate below passes. A failed or unavailable proxy must stop the session from gaining internet access. It must never fall back to open networking.

## 1. Complete session ownership

- Start one host proxy before each public-mode guest starts. Store its bound port in that guest's internal network configuration.
- For a Linux TAP guest, install the owned nftables policy before guest execution. Permit only the guest-to-proxy listener flow and deny all other guest input and forwarding.
- For a QEMU user-network guest, launch slirp with `restrict=on`, IPv6 disabled, and `guestfwd` from the fixed guest proxy address to the session's host listener.
- Pass the guest-visible endpoint to `smolvm-browser-session`. Confirm the running Chromium process has both `--proxy-server=<endpoint>` and `--proxy-bypass-list=<-loopback>`.
- Stop and release the proxy on normal stop, delete, failed startup, and host-process shutdown. Restart and reconnect must recreate the proxy and network policy before the browser resumes.
- Inject proxy bind, policy-install, and process-crash failures. Each failure must be short and actionable, and no guest request may escape through ordinary NAT.

## 2. Run the attack matrix in real guests

Use deterministic host-owned DNS and HTTP/TLS fixtures. Do not use the public internet as correctness evidence. Run the same candidate browser image with Linux Firecracker/TAP and QEMU/slirp; include macOS QEMU/HVF when release infrastructure can provide it.

Before running a real guest, keep the deterministic contract matrix green with `uv run pytest tests/runtime/test_public_egress.py tests/host/test_public_egress_proxy.py tests/host/test_public_egress_session.py tests/host/test_public_proxy_network.py tests/runtime/test_browser_proxy_plumbing.py tests/runtime/test_qemu_args.py`. Every denial fixture must name exactly one matching public control so the suite distinguishes policy enforcement from a broken transport. The proxy tests must also reject malformed or ambiguous HTTP framing and allow CONNECT only to the configured secure-tunnel port, currently 443.

For each backend, prove these positive controls:

- Public HTTP and HTTPS requests complete through the proxy.
- Public WebSocket and secure WebSocket connections complete through the proxy.
- A public redirect, subresource, and Service Worker fetch stay on the proxy path.

For each backend, prove these denials with both the browser result and destination-side evidence that no payload arrived:

- IPv4, IPv6, IPv4-mapped IPv6, loopback, private, link-local, multicast, unspecified, reserved, metadata, and other special-purpose destinations.
- Mixed public/private answers, DNS answer changes, rebinding, and CNAME chains ending in a denied address.
- Public-to-private redirects, private subresources, Service Worker requests, and WebSocket upgrades to denied destinations.
- Guest DNS sent anywhere except the controlled proxy path, direct IP connections, and raw-socket bypass attempts.
- New connections after the proxy is killed. They must fail closed without enabling open networking.

Repeat the lifecycle cases after stop/start and snapshot restore. Run simultaneous sessions and prove that one guest cannot reach another guest's proxy listener.

## 3. Build and smoke the exact image candidate

The browser session helper is embedded in the browser and Linux computer root filesystems, so changing it is image-affecting work.

1. Extend `.github/workflows/build-published-images.yml` to produce browser and Linux computer rootfs assets for native `amd64` and `arm64`. The current preset matrix does not publish those artifacts.
2. Upload one content-addressed candidate per architecture. Make the Firecracker and QEMU jobs download that exact candidate rather than consulting the published catalog.
3. Gate pull requests on Linux `amd64` Firecracker and QEMU browser smokes. Gate the image release on native `amd64` and `arm64` builds and real-VM smokes for both backends.
4. In each smoke, start a browser with the proxy endpoint, inspect the Chromium command line, and run the complete attack matrix from section 2.
5. Run the existing browser/computer command, file, live-view, stop/start, and cleanup smokes with no proxy argument to prove backward compatibility.

The helper protocol remains backward compatible, so its optional argument does not require an image-type version bump. `build_browser_rootfs` fingerprints the helper content through `_browser_session_sha256`, which forces a rebuild when the script changes. Bump the image type only if a future helper change rejects the old argument shape or changes persisted image semantics.

## 4. Pin and verify release artifacts

After both backend suites pass against the same candidate:

1. Choose a new `IMAGES_RELEASE_TAG` in `src/smolvm/images/published.py` and run the published-image build as a draft release.
2. Add browser and Linux computer entries to the published manifest, then copy every rootfs URL, size, and SHA-256 from the build output into `src/smolvm/images/published.py`.
3. Copy the standalone `smolvm-guest-agent-linux-<arch>.sha256` values into `src/smolvm/images/builder.py::_GUEST_AGENT_RELEASE_SHA256`. These pins are separate from the rootfs manifest.
4. Run `.github/workflows/smoke-published-images.yml` against the draft tag and make it smoke the new browser/computer assets on Firecracker and QEMU for `amd64` and `arm64`.
5. Verify the source-checkout path with `uv run smolvm ...`, which may build or use a local guest-agent binary.
6. Build and install the candidate wheel in a clean environment, then verify `smolvm ...` downloads the standalone guest-agent asset and the pinned rootfs.
7. Publish the draft image release only after its manifest and both SHA pin sets match the tested bytes. Do not tag the Python package before these checks pass.

## 5. Expose the API last

Only after sections 1 through 4 pass:

- Add `mode="public"` to Python `InternetSettings` and `{ mode: "public" }` to the TypeScript `NetworkPolicy`.
- Update the server schema, OpenAPI output, generated clients, persistence compatibility, and capability negotiation together.
- Make unsupported images return one short error that says how to install a compatible image.
- Switch OpenMuse from open to public explicitly. Never substitute open, off, or restricted mode when public mode is unavailable.

Keep the real-backend jobs gated or manually dispatched where the required virtualization is unavailable in ordinary pull-request CI. Their passing artifacts are still mandatory evidence for an image release and the public API flip.
