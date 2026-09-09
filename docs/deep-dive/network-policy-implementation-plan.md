# Minimal network policy implementation plan

SmolVM should start quickly and do exactly what its network settings promise. The first release will let callers turn outbound access off or limit it to specific IP addresses, while keeping commands and file operations usable.

Status: first-release implementation merged in PR #496. Controlled Linux packet tests and real KVM Firecracker lifecycle tests pass, including commands, file transfers, restart, and disk snapshot restore in off/restricted modes. Release preparation is checking actual firewall-install failures, installed-package examples, and startup/restore measurements. This is not a published release.

Implementation decisions:

- Explicit off/restricted modes are Firecracker-only; QEMU TAP keeps legacy domain support.
- Each managed NAT interface gets an owned nftables table with early forward/input checks. Replacement is one transaction, including removal of blanket forwarding permission. This avoids shared rule-handle discovery and makes stale connection state unable to bypass explicit policies.
- Open mode keeps existing private-network access, with earlier sandbox and IPv4 link-local isolation. No proxy or guest-image change was added.
- The SDK, lifecycle checks, controlled Linux test suite, CI dependency, and benchmark script are implemented. The full regression suite passes (2,183 passed, 21 skipped, 33 deselected). The controlled Linux namespace packet test passes against both source and the installed wheel, including IPv6 positive/negative controls. Firecracker and QEMU E2E passed in [run 34314767896](https://github.com/CelestoAI/SmolVM/actions/runs/34314767896); explicit off/restricted modes remain Firecracker-only. Latency results remain pending.

Run `python scripts/benchmark-network-policy.py --help` for the measurement entry point. Use the same script and controlled endpoint against baseline and candidate checkouts on a disposable Linux runner.

The E2E workflow has an opt-in `policy_release_validation` dispatch input. It builds baseline/candidate wheels, checks the documented examples against the candidate wheel, and saves raw timings and summaries as `network-policy-release-evidence`. Use `policy_samples=100` for release measurements; smaller values only smoke the harness. Baseline is `685b7bd52d0de8efb1c88fe8cf053d0d3b64d171`, immediately before PR #496. Both versions use the same image catalog, the default two virtual CPUs, 512 MiB RAM, and repeated baselines around each serial/concurrent matrix. Concurrency runs first. Completed samples are stopped immediately outside the measured spans; graceful-shutdown waiting is not part of this startup/restore benchmark.

## Shipping decision

Ship one small release on Linux with Firecracker NAT networking. Include Linux QEMU TAP only if the same implementation passes the live tests; otherwise reject the new restricted modes there until it does. Keep unrestricted networking as the default.

The release contains three modes, early validation, reliable firewall installation, persistence across restart/restore, and live packet tests. It does not contain a proxy, live policy updates, a policy service, or a new database table.

The public contract is outbound access. `off` prevents guest-initiated IP connections; it does not disable the trusted command/file control channel or claim to prevent data leaving through command output and file downloads.

## 1. API and compatibility

Extend the existing `InternetSettings` in `src/smolvm/types.py` rather than introducing a second policy object or top-level constructor argument:

```python
SmolVM(internet_settings={"mode": "open"})
SmolVM(comm_channel="vsock", internet_settings={"mode": "off"})
SmolVM(
    comm_channel="vsock",
    internet_settings={
        "mode": "restricted",
        "allowed_cidrs": ["203.0.113.10/32"],  # Illustrative destination.
    },
)
```

Add `mode: Literal["open", "off", "restricted"] | None = None` and `allowed_cidrs: tuple[str, ...] = ()` (list inputs remain accepted; JSON uses arrays). Missing mode preserves the old interpretation of `allowed_domains`; it is a compatibility case, not a fourth advertised mode.

Validation rules:

- No settings, or legacy `allowed_domains=["*"]`, means open.
- Explicit `restricted` requires at least one IPv4 address/range; normalize bare addresses to `/32` using `ipaddress`, and reject malformed ranges or IPv6.
- Explicit `open` and `off` reject nonempty CIDR lists. CIDRs without explicit `restricted` are an error.
- Explicit modes reject non-default domain settings. Do not combine two different destination semantics in v1.
- Non-default HTTP-method settings fail with an actionable error because they are not enforced.
- Preserve legacy domain settings on supported NAT backends, with their existing setup-time IPv4 resolution semantics. Document that they allow addresses, not verified hostnames. Do not route them through a new proxy.
- A requested restriction on an unsupported backend is an error, replacing the current warning-and-continue behavior.

Use shared policy parsing and compatibility validation at the public constructor before image preparation and again at the execution boundary. Unknown keys are rejected. Public SDK policy errors use `smolvm.ValidationError`; direct Pydantic model construction retains Pydantic errors. Policy collections are immutable tuples so caller mutation cannot change stored state. `model_copy(update=...)` bypasses model validation, so execution-boundary revalidation remains required.

Keep the existing serialized VM configuration as the persistence mechanism. Test loading old configurations without the new fields. Existing objects carrying unenforced method restrictions must produce an actionable validation error rather than silently gaining enforcement claims.

### Control-channel scope cut

Require a resolved vsock control channel for the new `off` and `restricted` modes in v1. Vsock is the existing direct VM control connection, independent of guest IP networking. Default channel selection can resolve to it; an explicit incompatible request must fail rather than silently change transport.

Reject workspace mounts, port forwards, and later port-exposure operations for these modes in v1. Some existing operations repair networking or require SSH; supporting their exceptions now would enlarge the policy contract. Commands and file transfer must pass over the existing guest agent. If either cannot, fix that narrow path or withhold the mode; do not open networking as a fallback.

Bridge, QEMU user-mode networking, libkrun, Windows guests, and macOS guests do not support the new restrictions. Unrestricted use remains available as before.

## 2. Firewall behavior

Modify `src/smolvm/host/network.py`; retain nftables and existing TAP ownership. Scope every new rule to SmolVM-managed interfaces so unrelated host traffic is unaffected.

| Mode | Guest-initiated traffic | DNS | IPv6 |
| --- | --- | --- | --- |
| Open | Existing internet behavior, with verified sandbox/metadata isolation | Existing behavior | Preserve existing behavior |
| Off | Deny all IP destinations, including the host | Denied | Denied |
| Restricted | Allow configured IPv4 destinations; deny all others | Resolver must be explicitly allowed | Denied |

Restricted v1 permits all ports/protocols at an allowed address. It is an IP policy, not an HTTP policy. There is no automatic DNS exception. Callers can use literal addresses, preconfigured names, or explicitly allow a resolver. Document the latter's broader permission; do not add port-specific rules in this release.

Implementation requirements:

1. Put mandatory isolation decisions ahead of generic accepts, established-connection accepts, and port-forward accepts. Inspect the actual complete chain ordering, not only the generated per-policy fragment.
2. Cover both forwarded packets and packets addressed to the Linux host. Use an input hook for the latter; the existing forward hook is insufficient.
3. Block traffic between managed sandboxes and to metadata destinations, including `169.254.169.254`, before destination allowances. Reject overlapping restricted ranges that would imply an exception to these mandatory blocks. Inventory any additional metadata routes actually supported by the deployment; do not claim a universal list.
4. Private remote services can be explicitly allowed in restricted mode. Do not block all private address space in open mode as part of this release; that is a separate compatibility decision.
5. Apply restricted/off rules and remove the TAP's blanket permission in one nft transaction. Failure must leave the old policy intact and propagate an error.
6. Change NAT setup so callers can install address translation without first granting blanket egress. No temporary open window during create, repair, or restore.
7. Share rule generation between sync and async paths. Keep execution wrappers separate and small.
8. Remove only the sandbox's owned policy resources during cleanup. Before an interface name is reused, ensure stale rules and connection-tracking state cannot grant old permissions. Scope any connection-state cleanup narrowly; never flush the host's global table.

The existing global established-connection accept and separately removed `allowed_taps` membership are specific review points. The current unit tests do not prove full-chain precedence or transaction safety.

## 3. Lifecycle integration

Update these paths in `src/smolvm/vm.py` together:

- Synchronous and asynchronous creation.
- `ensure_network_connectivity`, which currently calls NAT setup again.
- `_ensure_firecracker_network_for_restore` and any backend-specific restore path.
- Normal start/restart and cleanup.

Order: resolve backend/control channel → validate effective settings → prepare network resources → install complete policy → start/resume guest execution.

Policy-install failure must prevent guest execution. Clean resources created by the failed attempt, preserve resources owned by other VMs, and report the failure. A repair operation must never temporarily widen policy.

Persist and reapply the same effective policy on restart and restore. Do not offer changing policy on a running or stopped existing sandbox in v1; create a new sandbox to change it. Reconnection reads stored settings and cannot override them. Legacy domain settings retain their documented re-resolution behavior.

Do not retrofit new semantics into already-running sandboxes. Document that corrected baseline rules take effect when networking is reconciled or the sandbox is restarted. If shared-chain changes affect existing guests immediately, identify that explicitly during implementation and release validation.

## 4. Tests that gate shipping

### Fast tests

Extend `tests/runtime/test_internet_settings.py`, `tests/runtime/test_network.py`, `tests/vm/test_vm.py`, and facade tests with behavior-focused coverage:

- Mode validation, IPv4 normalization, contradictory settings, legacy loading, and unsupported-backend errors.
- Rejection after facade configuration merging, including dict and `VMConfig` entry points.
- One transaction includes blanket-permission removal and new rules.
- Complete chain precedence, input filtering, IPv6 denial, and sync/async agreement.
- Policy application precedes execution; failure prevents execution in create and restore.
- Repair, reconnect, and later operations cannot reopen networking.

### Live Linux tests

Add `tests/e2e/test_network_policy.py`, following the existing privileged network-lab pattern in `tests/e2e/test_bridge_networking.py`. Reuse the current image/runtime fixtures. Leave the existing untracked `tests/integration/network_policy/_assets/` files untouched.

Use isolated network namespaces and controlled HTTP/TCP/UDP responders. Avoid public services, real metadata endpoints, and external DNS as correctness dependencies. Use unique resource names and `finally` cleanup.

Required scenarios:

| Scenario | Passing result |
| --- | --- |
| Open | Controlled external endpoint works; neighboring sandbox and simulated metadata are denied |
| Off | TCP, UDP, DNS, IPv6, and host-service probes produce no successful application response or delivered payload |
| Restricted | Allowed endpoint responds; denied endpoint does not; explicit resolver behavior matches the contract |
| Control | Execute a command and upload/download a file under off and restricted |
| Lifecycle | Restart and snapshot restore retain policy before guest workload resumes |
| Failure | Inject firewall installation failure; guest workload never executes |
| Reuse | Delete/recreate with a different policy; previous flow/rules cannot grant access |
| Regression | Existing open-mode SSH, file transfer, mounts, and exposure still work |

Use both guest results and destination-side received-payload evidence. Probe with an attempted guest-side network reconfiguration as well, so policy is demonstrably outside guest control. Tests run only on disposable privileged Linux runners, not the developer's everyday network.

Add the required Firecracker job to the existing e2e workflow. QEMU TAP is advertised only after the same applicable matrix passes. A skipped privileged suite is not release evidence.

## 5. Performance and release gate

Before edits, record current creation-to-first-command, restore-to-first-command, and first successful network request on a fixed Linux runner and cached image. Separate image-download time from policy overhead.

Repeat after changes for open/off/restricted with one destination and a modest list (for example 32 entries), both serially and with eight concurrent starts. Use a small repeatable script and save raw timings; no benchmark framework.

Use 100 warm-image samples per serial mode for median and p95, and report concurrency results separately. Proposed initial gate: no repeatable open-mode p95 regression greater than 5% or 20 ms, whichever is larger. Validate runner noise with repeated baseline runs before treating this as a hard budget. Off/restricted should not perform DNS lookups, start helper processes, or make remote control-plane calls.

Ship when required live tests pass, performance stays within the agreed budget, and examples work from the installed package. Run the repository's normal required checks plus focused tests; do not alter unrelated code to satisfy optional scope.

No guest-image change is planned. If implementation requires one, follow the existing image build/smoke and release-pin checklist before tagging the package.

## 6. Delivery sequence

1. **PR 1 — contract and regression harness:** add the new settings, compatibility validation, and controlled test fixtures. Keep new modes explicitly unavailable until enforcement lands; never merge an accepted but unenforced mode.
2. **PR 2 — enforcement and lifecycle:** implement firewall changes and create/repair/restore/cleanup integration. Enable only supported, tested combinations. Land live tests alongside implementation.
3. **PR 3 — release preparation:** update networking documentation and constructor descriptions, record benchmarks, wire required CI, and smoke the installed package. Ship the release here.

If PR 1 is too small to justify a separate merge, combine it with PR 2. These are review boundaries, not an architecture requiring independently deployed components.

SDK-only configuration is sufficient for this release because the existing restrictions already enter through the SDK. Do not overload the CLI's existing `--network` attachment flag. A later CLI addition must preserve the project's noun-verb structure and use distinct outbound-policy options.

## Deferred follow-up: useful domain restrictions

Do not implement this until the first release ships. Validate demand using package installation, repository cloning, and model API calls. Then time-box a spike with an existing shared HTTP/HTTPS proxy: trusted per-sandbox policy identity, firewall-enforced bypass prevention, destination resolution checks, exact/wildcard domains, and blocked-destination diagnostics.

The spike must prove clients work, failures are understandable, and unrestricted startup is unaffected. It must define behavior for raw TCP, QUIC, shared hosting, and unavailable hostname information. Do not silently fall back to unrestricted access.

Defer live updates, HTTP-method restrictions, TLS decryption, secret injection, content inspection, dashboards, named policy resources, and per-sandbox proxy processes. No automatic package-registry exceptions or promise that allowing a service prevents uploading data to it.

The stopping point is a small, tested contract: **off means off, restricted IP access works, and normal sandbox startup remains fast.**
