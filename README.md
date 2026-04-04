<div align="center">

# SmolVM

**Run AI code, start a browser, and give AI agents an isolated workspace**


<img src="https://ik.imagekit.io/gradsflow/celestoai/logo/celesto%20cover%20low_vFigbRaJI.png">


[![CodeQL](https://github.com/CelestoAI/SmolVM/actions/workflows/github-code-scanning/codeql/badge.svg)](https://github.com/CelestoAI/SmolVM/actions/workflows/github-code-scanning/codeql)
[![Run Tests](https://github.com/CelestoAI/SmolVM/actions/workflows/pytest.yml/badge.svg)](https://github.com/CelestoAI/SmolVM/actions/workflows/pytest.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-orange.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-orange.svg)](https://www.python.org/downloads/)

[Docs](https://docs.celesto.ai) • [Examples](examples/) • [Slack](https://join.slack.com/t/celestoai/shared_invite/zt-3qc7h8gno-Nb5_PElEWHDNnGqdVzC~4Q)

</div>

---

SmolVM provides instant disposable computers to AI agents to run code, browser tasks and any other tasks that require a computer.


## Use cases

- Run untrusted code and agents in a sandbox environment.
- Start a virtual computer for agents to run tasks on (shell, browser, etc.)
- Keep one sandbox across multiple turns for stateful workflows.


## Quickstart

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

### Start a sandbox in Python

```python
from smolvm import SmolVM

with SmolVM() as vm:
    result = vm.run("echo 'You can run risky code here without affecting the actual machine.'")
    print(result.stdout.strip())
```


### Create a sandbox from CLI

```bash
smolvm create --name my-sandbox
```

Start a disposable browser session (you can see the browser UI in real time)

```bash
smolvm browser start --live
```

If you want to open the live browser view in your default browser:

```bash
smolvm browser open <session_id>
```

List sandboxes and dispose them when you are done.

```bash
smolvm browser list
smolvm browser stop <session_id>
```

Other useful CLI commands:

- `smolvm create --name my-sandbox`
- `smolvm create --os debian --name my-debian-sandbox`
- `smolvm ssh my-sandbox`
- `smolvm env list <vm_id>`
- `smolvm list`
- `smolvm stop my-sandbox`

## Examples

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
