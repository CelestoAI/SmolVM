---
name: cli-docs-guidelines
description: Review or write CLI documentation. Enforces progressive disclosure, logical command ordering, and plain-language explanations. Use when asked to "write CLI docs", "document commands", "review CLI reference", or "update command docs".
argument-hint: <file-or-section>
metadata:
  author: Celesto Team
  version: "1.0.0"
---

# CLI Documentation Guidelines

Review or write CLI docs following these principles. CLI docs serve two audiences: newcomers running their first command, and experienced users scanning for flags.

## Core Principles

All README Guidelines apply here. In addition:

### 1. Logical command ordering
Commands must appear in the order a user would run them. A command should never reference output or state from a command that appears later in the docs.

**Wrong** — `stop` appears before the user knows how to `list`:
```bash
smolvm stop <vm_id>
smolvm list
```

**Right** — create, inspect, then destroy:
```bash
smolvm create --name my-sandbox
smolvm list
smolvm stop my-sandbox
```

### 2. Introduce a concept before its flags
Show the base command before showing any flags or subcommands. Each flag is a new concept — don't introduce two flags in the same example unless they always go together.

**Wrong** — `--os` and `--name` are both new:
```bash
smolvm create --os debian --name my-debian-sandbox
```

**Right** — `--name` first, then a separate example for `--os`:
```bash
# Create a sandbox with a name
smolvm create --name my-sandbox

# Use a different OS image
smolvm create --os debian --name my-debian-sandbox
```

### 3. Show expected output after commands that produce it
When a command prints a value the user needs (an ID, a URL, a status), show it. The reader should never have to run the command to find out what it returns.

```bash
smolvm browser start --live
# Session: sess_a1b2c3
# Live view: http://localhost:6080
```

### 4. One topic per section
Don't mix sandbox lifecycle commands with browser session commands in the same section. Each distinct workflow gets its own heading.

### 5. Flags reference comes after prose explanation
Never lead with a flags table. Explain what the command does in plain language first, then list flags for readers who want to go deeper.

## Review Checklist

- [ ] Commands appear in the order a user would run them
- [ ] Every placeholder (`<vm_id>`, `<session_id>`) is introduced by a prior command or clearly labelled as "output from the previous step"
- [ ] Each code block introduces at most one new flag or subcommand
- [ ] Commands that print useful output show that output as a comment
- [ ] Conceptually distinct workflows (e.g. sandbox vs. browser) are in separate sections
- [ ] Flags/options table, if present, appears after the prose description
- [ ] No jargon (SSH, TAP device, CIDR, firecracker, QEMU) without a plain-language explanation on first use

## Output Format

For each violation found, output:

```
Line <N>: [rule violated]
  Current: <quote the problematic text>
  Fix: <suggested rewrite>
```

Then provide a revised version of any section that has more than one violation.
