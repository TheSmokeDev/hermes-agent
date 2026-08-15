---
title: Hermes Native Realtime HUD — Architecture Review Disposition and Freeze Receipt
status: architecture-frozen
date: 2026-08-11
final_review_verdict: FREEZE
implementation_authorized: false
---

# Architecture Review Disposition and Freeze Receipt

## 1. Final result

**`ARCHITECTURE_FROZEN`**

The frozen Intent PRD and companion architecture are aligned. The initial Fable
architecture review found no blocker and four bounded corrections. PR #65845 was
then inspected at exact source commits and reconciled as a reference-only
implementation. A fresh post-PR Fable delta review returned `FREEZE` with no
blockers, no corrections, and no additional scope cuts.

This receipt authorizes implementation planning only.

## 2. Initial Fable findings

| Finding | Disposition | Architecture change | Result |
|---|---|---|---|
| Pre-lease interruption required an impossible lease acknowledgment | accepted | lease acknowledgment is conditional on whether a lease opened; pre-lease path requires provider cancellation and spool destruction | resolved |
| Finalization-time reservation could not suppress pre-finalization streaming TTS | accepted | added admission-time exact-turn fence, retained finalization-time native reservation | resolved |
| `operator_only` tolerated an extra bot | accepted | member set must be exactly bound operator plus connected Hermes bot; every third principal fails closed | resolved |
| Provider fidelity premise was tested too late | accepted | added separately authorized representative-text qualification before dependent output slices | resolved |
| Cross-surface generalization note was speculative | accepted/cut | deleted from MVP data contract | resolved |
| Transcript-mirroring config key was speculative | accepted/cut | removed; current ordinary text delivery remains unchanged in MVP | resolved |

## 3. PR #65845 disposition

Exact PR receipt:

```text
NousResearch/hermes-agent#65845
base c0106e50e7ecedb3ce34e785d949725dc4e0e457
head b036d8be6d9786a7117777c8c3c2b40a84d2ca3b
```

Accepted as reference:

- one owner for a run's open event lifecycles;
- close open spans before terminal failure;
- fail-closed resume outcomes;
- identity-scoped compare-and-swap race cleanup;
- one terminal success/interrupt/failure projection;
- typed behavioral config and secrets-only environment values;
- explicit race and lifecycle tests.

Rejected for Talk:

- HTTP/SSE or CopilotKit dependency;
- client-supplied history;
- fresh `AIAgent` per voice turn;
- process-global dynamic tool registration/eager copied catalogs;
- adapter-side resume monkeypatch;
- AG-UI thread identity as durable Hermes authority;
- parked-worker registry as replacement approval authority;
- transport disconnect as authority to cancel Hermes-owned work.

The PR is not a runtime/package dependency. Its reproduced lockfile blocker is
therefore not a HUD architecture blocker.

## 4. Frozen minimum universal seam

Talk proves exactly five host-owned capabilities first:

1. consume-once exact-session attachment;
2. canonical serialized turn admission;
3. opaque durable finalization receipt;
4. typed transport-scoped interruption;
5. typed lifecycle observation and transport close.

No generic surface framework is authorized. AG-UI/Desktop may consume this seam
later only after exact canonical-session attachment replaces client-history and
fresh-agent ownership.

## 5. Final independent review

Artifact: `12-final-post-pr-delta-review-fable5.md`

- reviewer: Claude Fable 5;
- mode: read-only;
- verdict: `FREEZE`;
- blockers: none;
- corrections: none;
- additional cuts: none;
- permission denials: zero.

## 6. Frozen artifact hashes

```text
01-intent-prd.md
9fbf7652c31ec6b674427cff30a323efb37fe52aedf5bbcec45e093ee42e9e0e

05-companion-architecture.md
0ed3360a5cde187f6aea13e51a24ca893f755d0a8098fd9af5be7df248fec3b5

06-intent-architecture-reconciliation.md
77a2ca67736c013b27c5e7b595e4de630487224a94070c265e4c932ad398320b

10-pr-65845-agui-reference-assessment.md
5031ba51591c3fe547fbf88f65ab847d15cc311b62e0b769c64903249eea7092

12-final-post-pr-delta-review-fable5.md
3025865b419789dfdf75e76a1b37503a556c07aaf4246b2b9b202159b353ae14
```

## 7. Validation receipts

- PR assessment reconciliation checks: `15/15` passed;
- frozen architecture/reconciliation checks: `14/14` passed;
- `git diff --check`: passed;
- exact PR checkout remained clean;
- exact PR `uv lock --check`: failed as documented;
- exact PR frozen AG-UI export: failed as documented;
- no live provider qualification, native canary, install, restart, activation,
  merge, push, or publication occurred.

## 8. Next authorized gate

The next gate is a short, source-anchored implementation plan that decomposes the
frozen architecture into isolated Agent and Talk slices. It must begin with the
Talk-first five-capability seam and admission-time generic-TTS fence. It must not
implement future AG-UI/Desktop generalization.

Code execution, merge, install, restart, activation, and live canary remain
unauthorized until separately approved.
