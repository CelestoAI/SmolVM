# SmolVM Context

SmolVM is a secure, lightweight runtime for AI agents and tools, built on top of [Firecracker microVMs](https://firecracker-microvm.github.io/). It provides a high-level Python SDK and CLI to manage microVM lifecycles, networking, and command execution.


## 🚀 Project Overview

SmolVM is specifically designed to provide a secure "sandbox" for AI agents to execute code, browse the web, or perform system-level tasks safely.

- **Purpose:** Securely isolate untrusted code (like AI agent tools) using microVMs.
- **Technology Stack:**
    - **Language:** Python >= 3.10 (we use uv)
    - **Orchestrator:** Firecracker
    - **Build System:** Hatchling
    - **Dependencies:** Pydantic (data validation), Requests (API/Unix socket interaction), SSH (for command execution).
- **Architecture:**
    - **`smolvm.facade.VM`**: The user-facing class for easy VM interaction.
    - **`smolvm.vm.SmolVM`**: The core SDK orchestrator managing state, networking, and Firecracker processes.
    - **`smolvm.api`**: Low-level Firecracker API client.
    - **`smolvm.network`**: Manages TAP devices, NAT rules, and port forwarding.
    - **`smolvm.storage`**: SQLite-backed state management.
    - **`smolvm.host`**: Environment validation and Firecracker binary management.

## 🛠️ Building and Running

### Prerequisites
- **OS:** Linux (KVM support required).
- **System Setup:** Run the provided setup script to install dependencies and configure permissions.
  ```bash
  sudo ./scripts/system-setup.sh --configure-runtime
  ```

### Installation
```bash
pip install smolvm
```

### CLI Usage
The `smolvm` command provides demos and utility functions:
- `smolvm demo list`: List available demos.
- `smolvm demo simple`: Run a basic VM lifecycle demo.
- `smolvm cleanup --all`: Clean up all active and stale VMs.

### Quickstart Example
```python
from smolvm import VM

with VM() as vm:
    print(f"VM IP: {vm.get_ip()}")
    result = vm.run("echo 'Hello from SmolVM'")
    print(result.stdout.strip())
```

## 🧪 Development

### Key Commands
- **Testing:** `pytest` (runs the suite in `tests/`)
- **Linting & Formatting:** `ruff check .` or `ruff format .`
- **Type Checking:** `mypy src`
- **Build:** `hatch build`

### Design Conventions
- **Strict Typing:** All new code should be type-annotated and pass `mypy --strict`.
- **Async-ready but Sync-first:** The current SDK is synchronous for simplicity but designed with clear separation of concerns to allow future async support.
- **State Persistence:** All VM state is stored in `~/.local/state/smolvm/smolvm.db` by default.
- **Networking:** Each VM gets a dedicated TAP device and a private IP in the `172.16.0.0/16` range (default).

## 📂 Project Structure
- `src/smolvm/`: Main source code.
    - `facade.py`: High-level `VM` class.
    - `vm.py`: Core `SmolVM` orchestrator.
    - `build.py`: Image building logic (rootfs/kernel).
    - `network.py`: Linux networking setup (nftables/iproute2).
- `scripts/`: System-level setup and configuration.
- `tests/`: Comprehensive test suite covering all modules.
- `examples/`: Reference implementations and advanced usage.


## 🛡️ Security for Agents

When an LLM generates code, it's critical to execute it in an isolated environment. SmolVM provides:

- **Strong Isolation**: Unlike Docker, Firecracker microVMs use hardware virtualization (KVM), making it significantly harder for malicious code to escape to the host.
- **Controlled Networking**: Fine-grained control over guest networking, allowing you to restrict or monitor an agent's internet access.
- **Ephemeral Environments**: Easily spin up a fresh VM for every task and destroy it immediately after, ensuring no side effects persist.

## 🤖 Integration Patterns

### 1. Tool/Function Calling

You can wrap SmolVM as a tool for your AI agent.

```python
def execute_code_in_sandbox(code: str) -> str:
    """Tool for the agent to run shell code safely."""
    from smolvm import VM
    with VM() as vm:
        # Pre-install dependencies if needed
        # vm.run("pip install -r requirements.txt")
        result = vm.run(code)
        return result.stdout if result.exit_code == 0 else result.stderr
```

### 2. Long-running Agent Environments

For agents that need to maintain state across multiple turns:

1. Create a VM with a specific ID.
2. Perform actions.
3. Keep the VM running or stop/start it as needed.
4. Reconnect using `VM.from_id(vm_id)`.
