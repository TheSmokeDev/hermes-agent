# Intent PRD adversarial-review disposition

**PRD:** `01-intent-prd.md`  
**Review:** `02-intent-prd-adversarial-review-sol56.md`  
**Review agent:** Fresh independent Hermes `sol56` profile session `20260811_052546_2f38c6`  
**Original verdict:** `BLOCK`  
**Disposition:** All five blockers and seven important findings incorporated once; PRD status moved to `revised-after-adversarial-review`.

## Final independent re-review and intent freeze

A fresh read-only Claude Agent SDK worker session using requested and observed
model `claude-fable-5` independently re-read the revised PRD, original review,
and this disposition. The full receipt is recorded in
`04-intent-prd-final-adversarial-rereview-fable5.md` under SDK session
`8951873a-3fb6-4e8e-84a3-2387a5e8fa93`.

- Final verdict: `APPROVE_WITH_CORRECTIONS`
- Original findings: B1–B5 and I1–I7 all independently verified `RESOLVED`
  in the PRD text itself
- New blockers: none
- Required editorial corrections: five
- Corrections applied: all five in the intent-freeze change
- Current PRD status: `intent-frozen-after-final-review`
- Next authorized planning step: companion architecture

This freezes product intent only. It does not authorize implementation, merge,
installation, restart, production activation, or a live canary.

## Reviewer availability receipt

A fresh Hermes `fable5` review was attempted first in session
`20260811_052520_25303c`. Hermes resolved the intended native Anthropic route:

- provider: `anthropic`
- model: `claude-fable-5`
- credential: Claude Code OAuth
- custom/OpenRouter base URL: none

Anthropic rejected the turn because the subscription's included usage and
extra-usage balance were exhausted. No credential, provider, profile, billing,
or routing configuration was changed. The review therefore used a fresh
independent `sol56` Hermes agent as the authorized fallback.

## Blocker disposition

1. **B1 — speaker authorization:** Added exact adapter-authenticated platform
   principal binding to operator/profile/session/surface/generation; explicitly
   denied authority from provider attribution, transcript content, correlation
   IDs, and voiceprints; added unauthorized/stale/replay/duplicate negative
   acceptance steps.
2. **B2 — impossible interruption boundary:** Defined mutually exclusive normal
   drain and exact-lease interruption terminal outcomes; required prior canonical
   assistant durability; prohibited `COMPLETED` after `INTERRUPTED`; pinned the
   replacement across the barrier.
3. **B3 — transport cleanup vs durable work:** Separated transport-owned capture,
   provider, queue, playback, and reconnect cleanup from canonical Hermes-owned
   tools/delegation; explicitly denied process-local delegation survival claims
   across host restart.
4. **B4 — undefined spoken progress:** Removed spoken progress from MVP. MVP may
   render only exact persisted final canonical assistant text; future progress
   requires a separately specified Hermes-authored typed non-final contract.
5. **B5 — missing audio/data boundary:** Added explicit activation, visible
   capture, authenticated membership, least-data provider disclosure, provider
   isolation from tools/credentials/history, retention/redaction, shared-surface
   privacy, mute/detach, and non-sensitive negative canary requirements.

## Important-finding disposition

1. **I1 — universal parity overclaim:** Replaced broad "full parity" language
   with exact-session capability invariance and prohibited mid-conversation
   toolset/system-prompt mutation.
2. **I2 — Discord/Desktop scope conflict:** Named Discord the bounded MVP
   authority/transport proof; Desktop/Quick Entry remains a later product-surface
   gate and is not a prerequisite for the Discord canary.
3. **I3 — subjective latency:** Made first-canary latency observational, named
   required distributions, and prohibited realtime product-success claims until
   explicit budgets, percentile, sample count, and failure treatment are
   approved.
4. **I4 — physical audibility overclaim:** Replaced deterministic physical/audible
   claims with exact-lease adapter/device drain acknowledgment; retained human
   audibility only as bounded operator canary evidence.
5. **I5 — undefined rollback:** Added post-canary rollback acceptance from a
   fresh process, prior-runtime restoration, typed/session/history preservation,
   native-route disablement, and zero remaining realtime transport resources.
6. **I6 — unfalsifiable evidence:** Relabeled current-state claims as unverified
   starting hypotheses until exact immutable commits and scoped receipts are
   attached; explicitly bounded what input-only evidence cannot prove.
7. **I7 — consequential canary:** Replaced email/account switching with a
   deterministic harmless read-only dynamic tool and a separate bounded
   non-mutating delegation step. Sensitive or mutating demos are optional
   operator-approved follow-ups.

## Verification posture

- One adversarial prose review was performed.
- Concrete blockers and important findings were incorporated once.
- No second prose-review loop was run.
- Final verification is mechanical and evidence-based: document structure,
  contradiction search, diff hygiene, and traceability to this disposition.
- This disposition does not authorize implementation, commit, push, merge,
  installation, restart, or live canary.
