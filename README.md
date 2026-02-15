<div align="center">

# SmolVM

Secure runtime for AI agents, and tools -- free and open-source from Celesto AI 🧡

[Docs](https://docs.celesto.ai) •
[Examples](https://github.com/celestoai/smolvm/tree/main/examples)

</div>


## Install

```bash
# Install Python package
pip install smolvm

# Install pre-requisites and one time setup
sudo ./scripts/system-setup.sh --configure-runtime
```

## Quickstart

```python
from smolvm import VM

vm = VM()
vm.start()
print(f"VM running at {vm.get_ip()}")
result = vm.run("echo 'Command execution is ready'")
print(result.stdout.strip())
vm.stop()
```

Run with a context manager to automatically clean up the microVM after use:

```
from smolvm import VM

with VM() as vm:
    print(f"VM running at {vm.get_ip()}")
    result = vm.run("echo 'Command execution is ready'")
    print(result.stdout.strip())
```

Expose a guest app on localhost only (same machine access):

```python
from smolvm import VM

with VM() as vm:
    # Example: app in VM listening on port 8080
    host_port = vm.expose_local(guest_port=8080, host_port=18080)
    print(f"Open http://127.0.0.1:{host_port}")
```

Example script:

```bash
uv run python examples/install_openclaw.py
```


## 📄 License

Apache 2.0 License - see [LICENSE](LICENSE) for details.
