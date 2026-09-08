# Network policy integration tests

Check that a sandbox can contact approved websites and cannot bypass that list.
These tests use the production policy code. They do not enable the gated public
feature. See [results and release blockers](RESULTS.md).

## Wire tests

From this directory, install the isolated test dependencies:

```sh
uv sync --frozen --python 3.13
```

Run the real-socket HTTP/TLS tests:

```sh
uv run pytest -q --disable-warnings
```

This directory has its own pytest configuration and lockfile. Normal SDK test
collection excludes these optional-dependency tests. To select them from the
repository root, pass `-c tests/integration/network_policy/pyproject.toml` and
an explicit test path using an environment with these dependencies installed.

## Disposable Docker tests

The Linux suite needs Docker with `/dev/net/tun`. Each run uses a separate network
namespace and bounded capabilities, memory and CPU. **Never add host networking,
host filesystem mounts or `--privileged`.** Existing containers are left alone.

Run the kernel/firewall/worker suite:

```sh
./run-linux-tests.sh
```

Run the two-guest ARM64 QEMU suite (downloads SHA-pinned public images):

```sh
./run-qemu-tests.sh
```

The QEMU Dockerfile is self-contained; running the Linux suite first is optional.
Ignored `_assets/` and `.venv/` are local caches, never release artifacts.

## Native Linux smoke

Use a **disposable x86_64 Linux machine**, not a workstation or shared compute
host. These scripts change host networking and launch real guests as root.
They bypass only the public release gate, not enforcement or transport. On
failure they preserve process/state evidence: delete the entire machine and its
disks after collecting logs. Do not release uncertain placements by hand.

The successful GCP run used the smallest predefined nested-KVM-capable N1 Spot
machine (`n1-standard-1`), an auto-delete 15-GB standard disk and a two-hour
DELETE deadline. E2 lacked KVM. No service account/scopes or cloud firewall
changes were needed. Spot preemption is expected; never use customer data.

On Ubuntu 24.04, install QEMU, `iproute2`, `nftables`, `util-linux`, `zstd`, and
`uv`. Confirm `/dev/kvm` and `/dev/vhost-vsock` exist. Install this checkout and
its optional policy dependency into a Python 3.12+ environment. `--no-sources`
uses published `smolvm-core` instead of building the workspace Rust project:

```sh
uv pip install --no-sources '/opt/SmolVM[network-policy]'
```

With the checkout at `/opt/SmolVM`, run these **as root using that environment's
Python**. The first script downloads the explicitly pinned older image into
`/opt/policy-images` and uses `/opt/policy-native-state`:

```sh
SMOLVM_POLICY_NATIVE_TESTS=1 python /opt/SmolVM/tests/integration/network_policy/native_smoke.py
```

Then test creator-process exit, facade adoption and cached-channel fencing.
This uses the first script's image cache and `/opt/policy-adoption`:

```sh
SMOLVM_POLICY_NATIVE_TESTS=1 python /opt/SmolVM/tests/integration/network_policy/native_adoption.py
```

The older image is intentional: the checked current Pi amd64 Alpine image had
empty init/agent files. Passing with the older image does **not** validate the
current release. Both scripts must print a final `PASS`; preserve failures and
delete the disposable machine regardless of outcome.
