# Final adversarial re-review — Hermes Native Realtime HUD Intent PRD

## Review receipt

- **Reviewer:** Claude Fable 5 through the private bounded Claude Agent SDK worker
- **Review posture:** fresh independent read-only session
- **SDK session:** `8951873a-3fb6-4e8e-84a3-2387a5e8fa93`
- **Requested model:** `claude-fable-5`
- **Observed model:** `claude-fable-5`
- **Execution owner:** `claude-agent-sdk`
- **Billing source:** `unverified`
- **Termination:** `success`
- **Permission denials:** `0`
- **Tool evidence:** four calls to `mcp__hermes__read_file`; no write or native Claude tools
- **Files reviewed:** `01-intent-prd.md`, `02-intent-prd-adversarial-review-sol56.md`, and `03-intent-prd-review-disposition.md`
- **Files modified by reviewer:** none

> `sdk_cost_estimate_usd` was reported as `0.40728375`. This is SDK accounting metadata and is not evidence of separate billing.

## Final verdict

**APPROVE_WITH_CORRECTIONS**

## Executive rationale

Every original blocker and every important finding is substantively resolved **in the PRD text itself**. Each disposition claim was independently traceable to concrete revised language, and the reviewer found no case where the disposition claimed a fix absent from the PRD.

The authority boundary is consistent end to end: canonical Hermes is the sole reasoning, tool, approval, session, memory, and persistence authority; realtime remains bounded, untrusted audio/transcription/interruption/render transport. The formerly contradictory lifecycle requirements—drain versus interruption, close versus durable work, and spoken progress versus single-response authority—are now mutually exclusive, evidence-backed, and falsifiable.

The residual findings were five minor wording or precision defects. None reopened an authority leak, lifecycle deadlock, privacy gap, or acceptance contradiction. The reviewer recommended applying them in the intent-freeze change without another review cycle.

## Blocker verification matrix

| ID | Original blocker | Status | Verification in revised PRD |
|---|---|---|---|
| **B1** | Voice-turn authorization had no acceptance contract | **RESOLVED** | §8.2 admits an utterance only when Hermes binds an adapter-authenticated platform principal to the authorized operator, exact profile/session/surface, and current generation. Provider attribution, transcript content, IDs, and voiceprints grant no authority. §12.11 and §13 step 2 require unauthorized, stale, replayed, duplicated, wrong-session, and wrong-generation events to fail closed without creating a canonical turn. |
| **B2** | Barge-in required an impossible playback boundary | **RESOLVED** | §8.5 requires the prior canonical assistant turn to be durably terminal and defines exactly two mutually exclusive delivery outcomes: normal exact-lease drain or host-observed interruption acknowledgment. `INTERRUPTED` can never emit `COMPLETED`; the replacement remains pinned and is admitted once after the barrier. §§8.7 and 12.9 agree. |
| **B3** | Surface cleanup contradicted durable background work | **RESOLVED** | §6 promise 9, §§12.10 and 14.3, and §17 distinguish transport-owned capture/provider/audio/playback/reconnect work from canonical Hermes-owned tools and delegation. Surface close cannot redefine canonical work ownership, and the PRD no longer claims process-local work survives a Hermes host restart. |
| **B4** | Spoken progress created a second response authority | **RESOLVED** | §§5, 8.3, 8.4, and 18.1 remove spoken progress from MVP. MVP renders only the exact persisted final canonical assistant result. Future progress requires a separately specified, Hermes-authored, typed, auditable non-final contract that cannot claim approval, tool success, or finality. |
| **B5** | Audio/data security and privacy boundary was missing | **RESOLVED** | §9.1 defines explicit activation, visible capture, immediate mute/detach, authenticated membership and speaker authorization, least-data provider disclosure, provider isolation from Hermes credentials/tools/history/memory, retention and redaction policy, shared-surface privacy, and non-sensitive negative canary requirements. §§13 steps 2 and 11 enforce it. |

## Important-finding verification matrix

| ID | Original finding | Status | Verification in revised PRD |
|---|---|---|---|
| **I1** | Universal “full parity” overclaim | **RESOLVED** | §§1, 6.3, 10.1, 12.3, Gate A, and 20 use exact-session capability invariance. Voice neither adds nor removes capability and cannot mutate the toolset or system prompt mid-conversation. |
| **I2** | Discord MVP and Desktop HUD scope conflict | **RESOLVED** | §10.1 identifies Discord as the first bounded proof; §10.2 and Gate D make Desktop and Quick Entry later product-surface gates, not prerequisites for the Discord canary. |
| **I3** | No falsifiable latency success gate | **RESOLVED** | §14.2 makes first-canary latency observational, names required distributions and sample/failure evidence, and forbids declaring product success until explicit budgets, percentile, sample count, and failure treatment are approved. |
| **I4** | “Physical playback” exceeded observable evidence | **RESOLVED WITH EDITORIAL NOTE** | §§8.4, 8.7, 12, and 14.1 use exact-lease adapter/device drain acknowledgment and keep audibility as separate human canary evidence. One stale phrase remained in §17 and was corrected during freeze. |
| **I5** | Rollback was named but never accepted | **RESOLVED** | §12.14, §13 step 12, Gate E, and §14.3 require rollback from a fresh process, restoration of the exact prior reviewed runtime, typed/session/history preservation, native-route disablement, and zero residual realtime resources. |
| **I6** | Current-evidence claims were unfalsifiable | **RESOLVED** | §19 labels current-state statements as unverified hypotheses until exact immutable commits and scoped receipts are attached, and explicitly states what input-only evidence cannot prove. |
| **I7** | Canary combined consequential variables | **RESOLVED** | §§12.4–12.5 and §13 split a harmless read-only dynamic tool from a separate bounded non-mutating delegation step. Email, account switching, mutating control, and sensitive-data demos are optional operator-approved follow-ups, not MVP acceptance gates. |

## New findings

### Blockers

**None.**

The reviewer specifically searched for authority leaks, lifecycle deadlocks, cancellation and durability confusion, privacy gaps, scope creep, and unfalsifiable acceptance criteria. None remained at blocker severity.

### Minor corrections

1. **N1 — stale physical-playback phrase in §17.** Replace “physical playback evidence” with exact-lease adapter/device drain acknowledgment and keep physical audibility as human-confirmed canary evidence.
2. **N2 — `RECONNECTING` mislabeled terminal in §8.7.** It is transitional recovery state, not a terminal outcome.
3. **N3 — disjunctive delegated-work criterion in §12.5.** “Start or inspect” could pass without proving survival. Require starting bounded non-mutating work and observing continuation across voice-surface close or playback interruption.
4. **N4 — live approval path could remain unexercised.** A harmless read-only tool may not need approval. Either choose one that naturally exercises the existing approval ceremony or explicitly preserve deterministic approval-state testing and record that no live approval was required.
5. **N5 — speech before playback only implicitly serialized.** Explicitly state that speech finalized while a canonical turn is in flight and no playback lease exists remains pinned and is never injected mid-turn.
6. **N6 — optional schema-flavored “row” wording.** “Canonical row” and “assistant persistence rows” remain implementation-flavored but were optional in the original review and do not affect the verdict.

## Freeze corrections applied

The five required corrections were applied to `01-intent-prd.md` in the intent-freeze change:

1. §17 now uses exact-lease adapter/device drain acknowledgment and bounds human audibility to the canary.
2. §8.7 classifies `RECONNECTING` as transitional recovery state.
3. §12.5 requires starting delegated work and observing continuation across surface close or playback interruption.
4. §13 step 4 now distinguishes a naturally required live approval from deterministic approval-state proof when the harmless tool needs no approval.
5. §8.5 now pins speech finalized during an in-flight canonical turn with no playback lease and prohibits mid-turn injection.

The optional “row” wording was retained because it does not change authority, acceptance, or architecture.

## Final recommendation

**Product intent may be frozen, and companion architecture work may begin.**

This freezes intent only. It does not authorize implementation merge, installation, gateway restart, production activation, or a live canary. Those remain separately gated by the PRD.