# Security Policy

Thank you for helping keep SmolVM secure.

## Important Legal Note

SmolVM is licensed under the **Apache License 2.0**. See [LICENSE](LICENSE).

This security policy is provided on a **best-effort** basis and is for process clarity only.
It does **not** create any contractual, legal, or other binding obligation on maintainers.
In particular, this policy does **not** create:

- any duty to respond within a specific time,
- any duty to fix, backport, or disclose on a specific schedule,
- any warranty, guarantee, SLA, or support commitment,
- any entitlement to payment, bug bounty, or other compensation.

All software and updates remain provided **"AS IS"**, without warranties, as described in Apache-2.0.

## Supported Versions

SmolVM is currently pre-1.0. Security fixes are prioritized for:

| Version / Branch | Supported |
| --- | --- |
| Latest release tag | ✅ |
| `main` branch | ✅ (best effort) |
| Older release tags | ❌ |

If you are on an older version, please upgrade before reporting behavior that may already be fixed.

## Reporting a Vulnerability

Please **do not open public GitHub issues** for suspected vulnerabilities.

Use GitHub's private vulnerability reporting flow:

- **Private report:** https://github.com/CelestoAI/SmolVM/security/advisories/new

If that link is unavailable, open a minimal issue asking maintainers for a private contact channel (without sensitive details).

## What to Include in a Report

Please include as much of the following as possible:

- A clear description of the vulnerability and impact
- Affected version/commit and host environment (OS, architecture)
- Reproduction steps or proof-of-concept
- Expected vs. actual behavior
- Any suggested mitigation

## Response Expectations (Best Effort)

As a small team, we handle reports as capacity allows.
Our non-binding target process is:

1. Acknowledge report within **3 business days**
2. Triage and severity assessment within **7 business days**
3. Provide periodic updates when possible

Timelines may vary depending on complexity and maintainer availability.

## Disclosure Policy

We follow coordinated disclosure where possible:

- Please allow reasonable time for a fix before public disclosure
- We may publish security advisories for confirmed issues
- We are happy to credit reporters (unless anonymous credit is requested)

## Scope Notes

This policy covers vulnerabilities in this repository's code and release artifacts.

Out-of-scope (unless caused by SmolVM code):

- Vulnerabilities in third-party dependencies/upstream projects
- Host misconfiguration outside documented SmolVM setup
- Security findings without a realistic exploit path or impact

## Current SSH Trust Model (Important)

SmolVM is optimized for non-interactive local sandbox workflows. To reduce user
friction in ephemeral VM lifecycles, the current SSH path accepts unknown host
keys on first connection (Paramiko `AutoAddPolicy`).

### Impact

- This can allow man-in-the-middle attacks in untrusted network environments
  (CWE-295).
- SmolVM should therefore be treated as a **trusted-host / trusted-network**
  local runtime by default.

### Recommended Operational Guidance

- Prefer local-only usage on developer machines or trusted CI runners.
- Avoid exposing guest SSH endpoints to public or untrusted networks.
- If your environment requires strict host identity validation, add external
  network controls (private networking, firewall restrictions, bastion/proxy,
  or SSH key pinning policy at your deployment layer).
