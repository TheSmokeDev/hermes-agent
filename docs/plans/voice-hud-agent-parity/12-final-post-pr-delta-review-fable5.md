---
title: Hermes Native Realtime HUD — Final Post-PR Delta Review
status: complete
verdict: FREEZE
date: 2026-08-11
reviewer: Claude Fable 5
route: official Claude Code safe-mode, read-only
session_id: 150226a7-5bbb-4d2c-8c81-a9510f265858
permission_denials: 0
---

# Final Post-PR Delta Review — Hermes Native Realtime HUD

## Reviewer basis

All five pinned artifacts matched their SHA-256 hashes exactly. PR checkout
verified at head `b036d8be6…`; base `c0106e50e…` is its merge base; scope is 33
files, +5,968/-8.

Source spot-checks confirmed every load-bearing PR-assessment claim:

- `pyproject.toml` declares the `agui` extra while `uv.lock` contains no AG-UI
  metadata, so the lock blocker is real;
- `agui_adapter/session.py:436-503` constructs a fresh `AIAgent` per run;
- `server.py:143-243` uses client-supplied history and AG-UI `thread_id`;
- `resume_shim.py` process-globally wraps `build_turn_context` behind a context
  flag and identifies the cleaner gated-core-hook alternative;
- `approvals.py` fails closed on malformed, unknown, concurrent, and timed-out
  decisions with identity-scoped race cleanup;
- `server.py:299-344` uses compare-and-swap unregistration and best-effort
  disconnect interruption.

## 1. Verdict

**`FREEZE`**

## 2. Verified blockers

**None.**

- No authority bypass: the five-capability seam adds no tool, history, approval,
  or run-state surface and excludes AG-UI-specific machinery.
- No wrong-session, duplicate, or lost turn: pinned-replacement ordering preserves
  the frozen next-serialized-turn rule.
- No unsafe cancellation: transport interruption is explicitly scoped and does
  not imply canonical/background-work cancellation.
- No false terminal state: `CANONICAL_FINALIZED` remains distinct from
  drain-gated `COMPLETED`.
- No disclosure regression: any extra Discord principal, human or bot, fails
  closed.
- No unbounded leak: all reviewed queue, spool, ledger, and retention ceilings
  remain.
- No frozen-intent contradiction was found.

## 3. Prior corrections verified

All four were applied correctly and consistently:

1. pre-lease interruption requires provider cancellation and spool destruction;
   lease acknowledgment is required only if a lease opened;
2. attached turns are fenced before streaming-TTS setup, with final native
   reservation still occurring after canonical finalization;
3. `operator_only` means exactly the bound operator and connected Hermes bot;
4. bounded provider qualification occurs before dependent native-output slices
   and cannot weaken the fidelity gate.

## 4. Over-engineering check

No new cut is required.

The cross-surface generalization note and speculative transcript-mirroring config
key were removed. Latency remains observational. PR reference patterns remain
informative, not a new framework. No AG-UI-shaped abstraction may be built before
Talk consumes the host seam.

## 5. Five-capability seam

**Minimum-sufficient.** Each capability is required by a frozen promise:

1. consume-once exact-session attachment;
2. canonical serializer admission;
3. opaque durable finalization receipt;
4. scoped transport interruption;
5. typed lifecycle observation and transport close.

Removing any one breaks a frozen promise. Adding AG-UI protocol, client history,
tool registration, or run/approval registries would add authority or speculative
machinery and is correctly prohibited.

## 6. Freeze recommendation

Freeze the architecture. The PR delta adds exactly one structural clarification:
the five-capability Talk-first universal seam. It preserves frozen authority,
refuses a general surface framework, and accurately treats PR #65845 as a
reference rather than a dependency. The PR's lock failure is irrelevant to the
HUD architecture because none of its package/runtime code is imported. Updating
the architecture/reconciliation statuses and recording final hashes are the only
remaining mechanical steps. Implementation remains a separate authorization
gate.
