<div align="center">

# SmolVM

Secure runtime for AI agents, and tools -- free and open-source from Celesto AI 🧡

[Docs](https://docs.celesto.ai) •
[Examples](https://github.com/celestoai/smolvm/tree/main/docs/examples)

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


## 📄 License

Apache 2.0 License - see [LICENSE](LICENSE) for details.
