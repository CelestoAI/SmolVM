---
name: readme-guidelines
description: Review or write README content for open-source projects. Enforces progressive disclosure, jargon-free language, and single-concept code examples. Use when asked to "write README", "review README", "update README", or "check docs".
argument-hint: <file-or-section>
metadata:
  author: Celesto Team
  version: "1.0.0"
---

# README Guidelines

Review or write README content following these principles. The goal is easy onboarding for both newcomers and advanced users.

## Core Principles

### 1. Progressive disclosure of complexity
Structure content so readers can stop at any point and still have a working mental model. Each section should be usable on its own:
- Lead with the simplest outcome (one-liner, quickstart)
- Add detail in subsequent sections
- Advanced topics (integrations, internals, performance) come last
- Never require reading ahead to understand what's in front of you

### 2. One concept per code example
Each code block should demonstrate exactly one idea. If a snippet requires the reader to understand two or more new things simultaneously, split it.

**Wrong** — introduces sandbox creation AND environment variables at the same time:
```python
with SmolVM(env={"API_KEY": "secret"}) as vm:
    vm.run("curl $API_KEY")
```

**Right** — teaches sandbox creation first, env vars in a separate example:
```python
with SmolVM() as vm:
    vm.run("echo 'hello'")
```

### 3. Jargon-free language first, depth second
Explain every concept as if talking to a first-year CS student before using technical terms. Then go deeper if the reader needs it.

- Bad: "SSH host keys are accepted on first connection via TOFU"
- Good: "SmolVM automatically trusts new sandboxes on first connection to keep setup simple. (This is called trust-on-first-use, or TOFU — the same approach your browser uses for new websites.)"

### 4. Introduce before you use
Never use a value, flag, or identifier in a code block without explaining where it comes from. If a command prints a `session_id`, show that command *before* any command that takes `session_id` as input.

## Review Checklist

When reviewing a README, check each section against these rules:

- [ ] Tagline: does it describe a single, concrete outcome?
- [ ] Intro paragraph: can a newcomer understand it without prior context?
- [ ] Quickstart: does it follow install → configure → first run, in that order?
- [ ] Each code block: does it introduce exactly one new concept?
- [ ] Each new identifier (`<session_id>`, `<vm_id>`): is it introduced before it's used?
- [ ] Jargon: is every technical term explained in plain language on first use?
- [ ] Sections: does complexity increase monotonically top-to-bottom?
- [ ] Examples table: are entries grouped by audience (getting started vs. advanced)?
- [ ] Footer: does it duplicate links that already appear at the top?

## Output Format

For each violation found, output:

```
Line <N>: [rule violated]
  Current: <quote the problematic text>
  Fix: <suggested rewrite>
```

Then provide a revised version of any section that has more than one violation.
