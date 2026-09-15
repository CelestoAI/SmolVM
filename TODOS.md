# TODOs

## OpenMuse

### Automate expandable trace UI flows

**What:** Add a focused Playwright suite for collapsed, expanded, running, approval-paused, failed, cancelled, reconnecting, and mobile trace states.

**Why:** This implementation adds server and reducer coverage plus a manual browser pass. Dedicated visual interaction coverage should follow without expanding the first release.

**Context:** The trace UI lives beside each user message and uses nested native disclosure controls. Cover keyboard operation, focus stability, reduced motion, and bounded payload scrolling.

**Effort:** M

**Priority:** P2

**Depends on:** Expandable OpenMuse agent traces.

## Completed
