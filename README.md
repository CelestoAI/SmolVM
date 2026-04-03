<div align="center">

<img src="https://ik.imagekit.io/gradsflow/celestoai/logo/smolvm_Es-VK398Q.png?updatedAt=1775222245588" width=100px>


# SmolVM

**Run code, start a browser, and give AI agents an isolated workspace**

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

[Docs](https://docs.celesto.ai) • [Examples](examples/) • [Slack](https://join.slack.com/t/celestoai/shared_invite/zt-3qc7h8gno-Nb5_PElEWHDNnGqdVzC~4Q)

</div>

---

SmolVM is a Python SDK and CLI for running code and browser tasks inside disposable sandboxes. Use it when your app or agent needs a clean place to execute commands, open websites, or keep risky work away from your machine.

## What you can do

- Run untrusted code in a clean sandbox instead of on your host.
- Start a real browser session that you can automate or watch live.
- Plug SmolVM into agent tools for shell use, browser use, and computer-use workflows.

## Start here

1. Install the package.

```bash
pip install smolvm
```

2. Run the one-time setup for your machine.

```bash
smolvm setup
```

Linux may prompt for `sudo` during setup so it can install host dependencies and configure runtime permissions.

3. Check that the runtime is ready.

```bash
smolvm doctor
```

## Quickstart: Run a command in a sandbox

```python
from smolvm import SmolVM

with SmolVM() as vm:
    result = vm.run("echo 'Hello from the sandbox'")
    print(result.stdout.strip())
```

Default zero-config guests use Alpine. If you want a Debian guest instead:

```python
from smolvm import SmolVM

with SmolVM(os="debian") as vm:
    result = vm.run("echo 'Hello from the sandbox'")
    print(result.stdout.strip())
```

Run the full example:

```bash
python examples/quickstart_sandbox.py
```

## From the CLI: Start an isolated browser

Start a disposable browser session and print the local URLs you can use for automation or live view.

```bash
smolvm browser start --live --json
```

The JSON response includes the `session_id` plus local browser URLs. Use the session ID in the next commands.

The `cdp_url` can also be passed to external CDP clients. [examples/agent_tools/pydanticai_agent_browser.py](examples/agent_tools/pydanticai_agent_browser.py) shows a minimal flow that extracts the localhost port from that URL and hands it to `agent-browser --cdp`.

```bash
smolvm browser list
smolvm browser stop <session_id>
```

If you want to open the live browser view in your default browser:

```bash
smolvm browser open <session_id>
```

Other useful CLI commands:

- `smolvm create --name my-sandbox`
- `smolvm create --os debian --name my-debian-sandbox`
- `smolvm ssh my-sandbox`
- `smolvm env list <vm_id>`
- `smolvm list`
- `smolvm stop my-sandbox`

## Use cases

| Outcome | Start here |
| --- | --- |
| Run code in a clean sandbox | [examples/quickstart_sandbox.py](examples/quickstart_sandbox.py) |
| Start a disposable browser session | [examples/browser_session.py](examples/browser_session.py) |
| Let a model click and type on websites | [examples/agent_tools/computer_use_browser.py](examples/agent_tools/computer_use_browser.py) |
| Let PydanticAI drive the browser through `agent-browser` | [examples/agent_tools/pydanticai_agent_browser.py](examples/agent_tools/pydanticai_agent_browser.py) |
| Give an agent a shell tool | [examples/agent_tools/openai_agents_tool.py](examples/agent_tools/openai_agents_tool.py), [examples/agent_tools/langchain_tool.py](examples/agent_tools/langchain_tool.py), [examples/agent_tools/pydanticai_tool.py](examples/agent_tools/pydanticai_tool.py) |
| Keep one sandbox across turns | [examples/agent_tools/pydanticai_reusable_tool.py](examples/agent_tools/pydanticai_reusable_tool.py) |
| Pass env vars into the guest | [examples/env_injection.py](examples/env_injection.py) |

Advanced example: [examples/openclaw.py](examples/openclaw.py)

Each script shows its own `pip install ...` line when it needs extra packages.

## SDK or CLI?

Use the SDK when SmolVM is part of your app or agent loop and you want to create sandboxes from Python code. 

Use the CLI when you want to inspect the runtime manually, start a browser from the terminal, or script local workflows around `smolvm doctor`, `smolvm browser`, `smolvm env`, `smolvm create`, and `smolvm list`.

## Why isolation matters

SmolVM keeps risky work off your host by running it inside a separate guest system. On Linux it uses Firecracker microVMs, which are very small virtual machines backed by KVM. On macOS it uses QEMU. You still get a simple Python SDK and CLI, but the work happens in its own environment instead of sharing your main machine directly.

## Security notes

SmolVM is built for local, agent-style workflows. By default, SSH host keys are accepted on first connection to keep setup simple. Use it on trusted machines and networks, and avoid exposing guest SSH endpoints publicly without extra controls. See [SECURITY.md](SECURITY.md) for the full policy and scope.

## Performance

Typical lifecycle timings (p50) on a standard Linux host:

| Phase | Time |
| --- | --- |
| Create + Start | ~572ms |
| SSH ready | ~2.1s |
| Command execution | ~43ms |
| Stop + Delete | ~751ms |
| Full lifecycle (boot -> run -> teardown) | ~3.5s |

Run the benchmark yourself:

```bash
python3 scripts/benchmarks/bench_subprocess.py --vms 10 -v
```

Measured on AMD Ryzen 7 7800X3D (8C/16T), Ubuntu Linux, KVM/Firecracker backend.

## More

- [Docs](https://docs.celesto.ai)
- [Examples](examples/)
- [Security](SECURITY.md)
- [Contributing](CONTRIBUTING.md)
- [Slack](https://join.slack.com/t/celestoai/shared_invite/zt-3qc7h8gno-Nb5_PElEWHDNnGqdVzC~4Q)


## 📄 License

Apache 2.0 License - see [LICENSE](LICENSE) for details.

---
<div align="center">
Built with 🧡 in London by <a href="https://celesto.ai">Celesto AI</a>
</div>
