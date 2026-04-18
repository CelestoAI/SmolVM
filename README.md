<div align="center">

# SmolVM

#### Secure, isolated computers that AI agents can use to browse, run code, and get real work done. 


<img src="https://ik.imagekit.io/gradsflow/celestoai/logo/celesto%20cover%20low_vFigbRaJI.png">

[![CodeQL](https://github.com/CelestoAI/SmolVM/actions/workflows/github-code-scanning/codeql/badge.svg)](https://github.com/CelestoAI/SmolVM/actions/workflows/github-code-scanning/codeql)
[![Run Tests](https://github.com/CelestoAI/SmolVM/actions/workflows/pytest.yml/badge.svg)](https://github.com/CelestoAI/SmolVM/actions/workflows/pytest.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-orange.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-orange.svg)](https://www.python.org/downloads/)

[Quick start](#quickstart) • [Examples](#examples) • [Features](#features) • [Performance](#performance) • [Docs](https://docs.celesto.ai) • [Community Slack](https://join.slack.com/t/celestoai/shared_invite/zt-3qc7h8gno-Nb5_PElEWHDNnGqdVzC~4Q) 

</div>

---

SmolVM gives AI agents their own disposable computer. Each sandbox is a lightweight virtual machine that boots in seconds, runs any code or command you throw at it, and disappears when you're done — nothing touches your host.


## Features

- **Sub-second boot** — VMs ready in ~500 ms.
- **Hardware isolation** — Stronger security than containers.
- **Network controls** — Domain allowlists for egress filtering.
- **Browser sessions** — Full browser agents can see and control.
- **Host mounts** — Give sandboxes read access to local directories.
- **Snapshots** — Save and restore VM state instantly.
- **OpenClaw** — GUI Linux apps inside a sandbox.


## Use cases

- **Run untrusted code safely.** Execute AI-generated code in an isolated sandbox instead of on your machine.
- **Give agents a browser.** Spin up a full browser session that agents can see and control in real time.
- **Let agents read your project.** Mount a local directory so agents can explore your codebase inside a sandbox.
- **Keep state across turns.** Reuse the same sandbox throughout a multi-step workflow.


## Quickstart

Install SmolVM with a single command:

```bash
curl -sSL https://celesto.ai/install.sh | bash
```

This installs everything you need (including Python tooling), configures your machine, and verifies the setup.

<details>
<summary>Manual installation</summary>

```bash
pip install smolvm
smolvm setup
smolvm doctor
```

On supported Linux and macOS systems, `pip install smolvm` also pulls in the matching `smolvm-core` wheel automatically. Most users do not need Rust installed.

Linux may prompt for `sudo` during setup so it can install host dependencies and configure runtime permissions.

</details>

For golden-AMI builds, two-stage deploys, pinning the Firecracker version, and other non-default install paths, see [docs/installation.md](docs/installation.md).

### Start a sandbox in Python

```python
from smolvm import SmolVM

with SmolVM() as vm:
    result = vm.run("echo 'Hello from the sandbox!'")
    print(result.stdout.strip())
```

The `with` block creates a sandbox, runs your command inside it, and tears the sandbox down automatically when the block exits.


### Start a sandbox from the CLI

Create a sandbox, check that it's running, then stop it:

```bash
smolvm create --name my-sandbox
# my-sandbox  running  172.16.0.2

smolvm list
# NAME         STATUS   IP
# my-sandbox   running  172.16.0.2

smolvm stop my-sandbox
```

Open a shell inside a running sandbox:

```bash
smolvm ssh my-sandbox
```


## Browser sessions

SmolVM can also start a full browser inside a sandbox. This is useful when agents need to navigate websites, fill out forms, or take screenshots.

Start a browser session with a live view you can watch in your own browser:

```bash
smolvm browser start --live
# Session:   sess_a1b2c3
# Live view: http://localhost:6080
```

Open the URL to watch the browser in real time. When you're done, list and stop sessions:

```bash
smolvm browser list
smolvm browser stop sess_a1b2c3
```

See [examples/browser_session.py](examples/browser_session.py) for the Python equivalent.


## Network controls

By default, sandboxes have full internet access. You can restrict which domains a sandbox can reach by passing `internet_settings`:

```python
from smolvm import SmolVM

vm = SmolVM(internet_settings={
    "allowed_domains": ["https://api.openai.com"],
})

vm.run("curl https://api.openai.com/v1/models")    # allowed
vm.run("curl https://evil.com/exfiltrate")         # blocked
```

See [docs/concepts/network-egress-controls.md](docs/deep-dive/network-egress-controls.md) for how it works under the hood.


## Mount host directories

You can give a sandbox read access to a folder on your machine. This is useful when an agent needs to work with an existing project without copying files back and forth.

```bash
smolvm create --mount ~/Projects/my-app
smolvm ssh my-sandbox
ls /workspace   # your host files appear here
```

The host folder is read-only — the sandbox can read every file, but changes stay inside the sandbox and never touch the originals. If the agent creates or edits files under `/workspace`, those changes live only in the VM's overlay layer.

Mount at a custom path, or mount multiple directories:

```bash
smolvm create --mount ~/Projects/my-app:/code --mount ~/data:/mnt/data
```

The same works from Python:

```python
from smolvm import SmolVM

with SmolVM(mounts=["~/Projects/my-app"]) as vm:
    result = vm.run("ls /workspace")
    print(result.stdout)
```

> **Note:** This feature is read-only for now. Any changes you make inside the sandbox do not travel back to the host. Write-back support is planned for a future release.


## Examples

### Getting started

| What you'll learn | Example |
| --- | --- |
| Run code in a sandbox | [quickstart_sandbox.py](examples/quickstart_sandbox.py) |
| Start a browser session | [browser_session.py](examples/browser_session.py) |
| Pass environment variables into a sandbox | [env_injection.py](examples/env_injection.py) |

### Agent framework integrations

These examples show how to wrap SmolVM as a tool for popular agent frameworks, so an AI model can run shell commands or drive a browser through your sandbox.

| Framework | Example |
| --- | --- |
| OpenAI Agents | [openai_agents_tool.py](examples/agent_tools/openai_agents_tool.py) |
| LangChain | [langchain_tool.py](examples/agent_tools/langchain_tool.py) |
| PydanticAI — shell tool | [pydanticai_tool.py](examples/agent_tools/pydanticai_tool.py) |
| PydanticAI — reusable sandbox across turns | [pydanticai_reusable_tool.py](examples/agent_tools/pydanticai_reusable_tool.py) |
| PydanticAI — browser automation | [pydanticai_agent_browser.py](examples/agent_tools/pydanticai_agent_browser.py) |
| Computer use (click and type) | [computer_use_browser.py](examples/agent_tools/computer_use_browser.py) |

### Advanced

| What it does | Example |
| --- | --- |
| Install and run OpenClaw inside a Debian sandbox with a 4 GB root filesystem | [openclaw.py](examples/openclaw.py) |

Each script shows its own `pip install ...` line when it needs extra packages.


## Security

SmolVM automatically trusts new sandboxes on first connection to keep setup simple. This is safe for local development, but you should not expose sandbox network ports publicly without extra controls. See [SECURITY.md](SECURITY.md) for the full policy and scope.


## Performance

Median lifecycle timings on a standard Linux host:

| Phase | Time |
| --- | --- |
| Create + Start | ~572 ms |
| Ready to accept commands | ~2.1 s |
| Command execution | ~43 ms |
| Stop + Delete | ~751 ms |
| **Full lifecycle (boot, run, teardown)** | **~3.5 s** |

Run the benchmark yourself:

```bash
python3 scripts/benchmarks/bench_subprocess.py --vms 10 -v
```

Measured on AMD Ryzen 7 7800X3D (8C/16T), Ubuntu Linux. SmolVM uses [Firecracker](https://firecracker-microvm.github.io/), a lightweight virtual machine manager built for running thousands of secure, fast micro-VMs.


## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) to get started.


## License

Apache 2.0 — see [LICENSE](LICENSE) for details.

---
<div align="center">
Built with 🧡 in London by <a href="https://celesto.ai">Celesto AI</a>
</div>
