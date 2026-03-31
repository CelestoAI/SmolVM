# PydanticAI Agent Browser Example

Drive a SmolVM browser from PydanticAI using the `agent-browser` CLI.

## Flow Diagram

```mermaid
flowchart TD
    A[User runs script with prompt] --> B[PydanticAI Agent initialized]
    B --> C[Agent plans browser automation steps]
    
    C --> D[Start SmolVM browser session]
    D --> E{Browser ready?}
    E -->|Yes| F[Get CDP port for remote control]
    E -->|No| X[Error handling]
    
    F --> G[Navigate to target URL]
    G --> H[Take page snapshots]
    H --> I[Interact with page elements]
    I --> J[Scroll / click / extract data]
    
    J --> K[Capture final screenshot]
    K --> L[Get page title & URL]
    L --> M[Stop browser session]
    
    M --> N[Return summary to user]
    N --> O((Done))
    
    X --> M

    style A fill:#e1f5fe
    style O fill:#c8e6c9
    style D fill:#fff3e0
    style M fill:#fff3e0
    style N fill:#c8e6c9
```

## What It Does

1. **Input** — User provides a prompt (e.g., "open amazon.com and tell me trending products")
2. **Orchestration** — PydanticAI agent uses `run_host_bash` tool to execute SmolVM and agent-browser CLI commands
3. **Browser lifecycle** — Start isolated microVM browser → control via CDP → stop when done
4. **Output** — Screenshot, page summary, title, and URL returned to user

## Prerequisites

```bash
pip install smolvm pydantic-ai
brew install agent-browser  # or npm install -g agent-browser
agent-browser install
export OPENAI_API_KEY=...
smolvm doctor
```

## Usage

```bash
# Run the built-in demo (opens BBC News)
python examples/agent_tools/pydanticai_agent_browser.py

# Custom prompt
python examples/agent_tools/pydanticai_agent_browser.py --input "Open example.com"
```
