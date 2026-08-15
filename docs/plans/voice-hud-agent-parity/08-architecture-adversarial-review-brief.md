---
title: Hermes Native Realtime HUD — Minimum-Sufficient Architecture Review Brief
status: issued-for-independent-review
date: 2026-08-11
reviewer: Claude Fable 5
review_mode: read-only
---

# Minimum-Sufficient Architecture Review Brief

## Exact artifacts under review

| Artifact | SHA-256 |
|---|---|
| `01-intent-prd.md` | `9fbf7652c31ec6b674427cff30a323efb37fe52aedf5bbcec45e093ee42e9e0e` |
| `05-companion-architecture.md` | `33e53b0d0f3c584802ed3e6a43db569ad183936f0dda64fe3797a1dedfad6073` |
| `06-intent-architecture-reconciliation.md` | `64181dd71972fe00efd981f3612964afc0535d28de39d872c43176274b6ed650` |
| `07-architecture-source-assessment-receipt.md` | `2c7fc9d4dc45c53a95dbe909ef5a438f6d3db54e4190ba71a64de12d03c806ad` |

All paths are relative to:

```text
docs/plans/voice-hud-agent-parity/
```

## Reviewer mandate

Perform a fresh, independent architecture/security/concurrency review. The frozen
Intent PRD is product authority. The companion architecture must be sufficient to
begin a bounded Discord MVP implementation without creating a second agent,
weakening authorization, losing turns, producing false completion, or making
rollback impossible.

This is deliberately a **minimum-sufficient** review. Do not reward document size
or ask for speculative architecture.

## Required severity classes

### BLOCKER — must resolve before implementation

Use only when the current architecture would permit or require one of:

- authority bypass or a second agent/tool executor;
- wrong-session/wrong-speaker admission;
- duplicate/lost/reordered canonical turns;
- provider-authored response becoming canonical;
- generic TTS claiming a native-owned turn;
- false `COMPLETED`/`INTERRUPTED` state;
- unsafe cancellation of canonical/background work;
- sensitive shared-surface disclosure;
- unbounded or unowned transport work that prevents safe cleanup;
- no production-reachable path or no reversible rollback;
- direct contradiction with the frozen Intent PRD.

Every blocker must cite exact artifact/source evidence, concrete failure path,
smallest remediation, and one discriminating test.

### IMPORTANT — should correct in architecture before implementation

Use for a concrete ambiguity likely to cause incompatible implementations or a
security/concurrency bug, but which does not yet prove a blocker.

### IMPLEMENTATION-DECIDED

Use when the architecture already fixes owner, authority, lifecycle, and failure
semantics and the remaining choice is best made while writing/testing the first
concrete slice. These findings must **not** block architecture freeze.

Examples: internal class names, exact helper placement, private callback shape,
small queue tuning below a reviewed ceiling, and refactoring revealed by tests.

### POST-MVP / CUT

Use for Desktop/Quick Entry, wake phrase, spoken approval, future providers,
spoken progress, broad abstractions, general framework extraction, performance
optimization without evidence, or hardening that does not protect the bounded
Discord proof. Explicitly recommend deletion/deferment rather than turning these
into pre-implementation gates.

## Required review questions

1. Does every authority boundary have exactly one owner and a production-reachable
   path?
2. Can native reservation actually occur before every generic TTS/attachment
   branch?
3. Is canonical-text fidelity safe and minimally sufficient, or has the document
   over-designed it?
4. Are `COMPLETED` and `INTERRUPTED` mutually exclusive under races?
5. Does pinned replacement ordering preserve strict role alternation and typed
   turn ordering?
6. Can close/reconnect/barge-in leak transport work or cancel Hermes-owned work?
7. Is Discord speaker/listener authorization exact and minimally scoped?
8. Are retention/config/rollback rules sufficient without speculative machinery?
9. Which architecture sections or requirements should be deleted, simplified, or
   deferred until implementation evidence exists?
10. Is the architecture ready to freeze after bounded corrections, or does it need
    a redesign?

## Source anchors available for spot-checking

Read only what is necessary:

- `gateway/realtime_voice_invocation.py`
- `gateway/realtime_voice_messaging_host.py`
- `gateway/realtime_voice_controller.py`
- `gateway/platforms/base.py`
- `plugins/platforms/discord/adapter.py`
- `agent/realtime_voice_provider.py`
- Talk source facts summarized in `07-architecture-source-assessment-receipt.md`

Do not request implementation, edit files, run commands, inspect secrets, or
review unrelated Hermes/Talk architecture.

## Output contract

Return under 12,000 characters:

1. `APPROVE`, `APPROVE_WITH_CORRECTIONS`, or `REQUEST_CHANGES`.
2. Verified blockers.
3. Important corrections.
4. Implementation-decided items that must not block freeze.
5. Post-MVP/cut items and any current over-engineering to remove.
6. Smallest sufficient architecture after corrections.
7. Exact tests required before the first implementation slice can claim success.
8. A one-paragraph freeze recommendation.

Do not ask for broad refactors, prose expansion, generic future-proofing, or work
outside the frozen Discord MVP.
