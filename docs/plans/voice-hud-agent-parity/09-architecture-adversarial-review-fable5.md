---
title: Hermes Native Realtime HUD — Independent Architecture/Security/Concurrency Review
status: complete-pre-pr-65845-reference-not-final-freeze-gate
verdict: APPROVE_WITH_CORRECTIONS
date: 2026-08-11
reviewer: Claude Fable 5
route: official Claude Code safe-mode, read-only
session_id: ac8ce8a9-ca5c-4c58-b7d1-82bc8451b517
permission_denials: 0
---

# Independent Architecture/Security/Concurrency Review — Native Realtime HUD

## Reviewer basis

All four artifacts matched their pinned SHA-256 hashes. Worktree commits matched
all five pinned source snapshots (`d03d60fe3`, `faf4f4559`, `86c756de8`,
`28d7068`, `99d8f2a`). Source seams spot-checked:
`realtime_voice_invocation.py`, `realtime_voice_messaging_host.py`,
`realtime_voice_controller.py` (`faf4f455`), `platforms/base.py`,
`streaming_tts_consumer.py`, `run.py` streaming-TTS setup, Discord adapter TTS
surface, and the `86c756de` diff.

The reviewer confirmed that `86c756de` is a two-line sibling change that flips
host ingress from `MessageType.TEXT` to `MessageType.VOICE`.

## 1. Verdict

**`APPROVE_WITH_CORRECTIONS`**

## 2. Verified blockers

**None.**

Every candidate blocker class was checked and is either structurally prevented
or explicitly mandated for correction by the architecture itself:

- No second agent: provider gets zero tools, automatic response disabled,
  unknown tool/output events fail closed. The controller validates
  `allow_tools is False` on the claimed request.
- Generic-TTS claim of a native turn: the only current trigger is the quarantined
  `86c756de` `MessageType.VOICE` flip. The ingress base synthesizes
  `MessageType.TEXT`; generic audio paths gate on VOICE type. The quarantine is
  load-bearing and correct, subject to Important Correction 2.
- False `COMPLETED`: the current prototype emits canonical `COMPLETED` before
  native output, but the architecture already mandates the
  `CANONICAL_FINALIZED` split.
- Duplicate/lost turns: consume-once factory, permit/replay ledger, one-slot pin,
  and visible overflow align with frozen PRD 8.5.
- Unsafe cancellation/leaks: the architecture gives every transport resource one
  owner and retained close ownership.
- Production reachability: canonical ingress is reachable. Delivery arbiter and
  Discord PCM sink are honestly declared as new seams.
- No frozen-PRD contradiction was found.

## 3. Important corrections

### 3.1 Pre-lease barge-in acknowledgment semantics

**Finding:** The architecture requires an active playback lease and lease
interruption acknowledgment for `INTERRUPTED`, while also permitting interruption
during `PROVIDER_RENDERING`/`FIDELITY_VALIDATED`, before a lease exists. Two
implementations could diverge or one could wait forever for an impossible lease
receipt.

**Smallest correction:** Provider response-cancellation acknowledgment is always
required. Playback-lease interruption acknowledgment is required only if a lease
was opened. Pre-lease interruption destroys the spool and requires no lease
acknowledgment.

**Discriminating test:** Speech-start during fidelity validation reaches
`INTERRUPTED` without opening a lease and destroys the spool.

### 3.2 Admission-time fence for pre-finalization streaming TTS

**Finding:** Finalization-time native reservation is too late to suppress the
Gateway streaming-TTS consumer, which can begin during generation. Discord does
not currently override `supports_streaming_tts`, but relying on that current
accident is not an architecture guarantee.

**Smallest correction:** A native-attached turn is fenced at admission. Either it
never becomes VOICE-typed for generic routing, or its exact attachment claim
suppresses streaming-TTS setup and whole-file auto-TTS for the exact turn key.
Finalization-time reservation still owns native provider delivery.

**Discriminating test:** Attached session with `/voice on` and a VOICE-typed event
invokes zero `begin_streaming_tts` and zero `text_to_speech_tool` calls.

### 3.3 `operator_only` must reject every extra principal

**Finding:** The draft explicitly rejected extra humans but could tolerate another
bot. Another bot can record or relay playback and is a disclosure recipient.

**Smallest correction:** Member set must be exactly `{bound operator, connected
bot}`. Any other principal—human or bot—fails closed.

**Discriminating test:** A third bot present at reservation or pre-playback
revalidation produces `NATIVE_FAILED` before PCM release.

### 3.4 Early provider fidelity qualification

**Finding:** The fidelity architecture is sound but depends on the unverified
empirical premise that `gpt-realtime-2.1` returns terminal output transcripts
byte-equal to representative canonical UTF-8, including Markdown, punctuation,
and newlines. The current slice order would build dependent work before learning
whether the provider qualifies.

**Smallest correction:** Add a bounded, separately authorized provider
qualification probe before playback-sink and barge-in dependent slices. If the
provider cannot meet exact fidelity, block the MVP rather than weakening the
frozen gate.

**Discriminating test:** Representative canonical texts round-trip with
byte-exact terminal transcripts at a product-owner-accepted pass rate before the
Discord playback slice begins.

## 4. Implementation-decided items that must not block freeze

- Queue/spool/timeout tuning below reviewed ceilings.
- Replay-ledger and pin data structures.
- Exact mechanism by which the pin reserves the next serialized position; the
  ordering rule is already fixed.
- Redundant in-process `sha256(canonical_text)`—keep or remove while implementing
  the explicit-response slice.
- Class/callback/module naming, evidence-event taxonomy, reconnect backoff, and
  internal Talk session split.
- Whether pre-lease interruption reuses `INTERRUPTING` or a distinct internal
  state after the acknowledgment rule is fixed.

## 5. Post-MVP/cut

- Delete the cross-surface generalization note in §6.1. Desktop/Quick Entry
  returns in the later Desktop phase.
- Delete the speculative transcript-mirroring configuration key. Preserve current
  ordinary text delivery in the MVP; no new mirroring machinery is needed.
- Keep latency observations as observational logging only; do not turn them into
  pre-freeze instrumentation requirements.
- Keep wake phrase, spoken approval, spoken progress, additional providers, and
  background-focus speech out of the MVP.
- No further cuts were warranted. The reviewer found no broad framework
  extraction, invented latency budgets, or unnecessary provider framework.

## 6. Smallest sufficient architecture after corrections

Consume-once contextual attachment capability → host-serialized canonical
admission with replay/generation fencing → unchanged canonical Hermes turn →
consume-once finalization receipt → admission-time native fence plus
finalization-time atomic delivery reservation → digest-bound tools-off explicit
provider render → bounded pre-play fidelity gate → adapter-minted exact playback
lease → evidence-reduced terminal state with lease-linearized
`COMPLETED`/`INTERRUPTED` exclusivity → one-slot pinned replacement → strict
`operator_only` disclosure → bounded memory-only retention and
external-manifest rollback.

Nothing else is needed for the bounded Discord proof.

## 7. Exact first-slice success tests

1. Installed-plugin cross-repository test at exact Agent/Talk commits:
   `/talk core join` admits one exact durable-session user turn and one terminal
   assistant row; second factory consumption fails.
2. Unauthorized principal, stale generation, replayed final, duplicate final, and
   wrong-session events create zero canonical rows and zero turn leases.
3. Attached turn invokes no streaming-TTS consumer, whole-file auto-TTS,
   `play_tts`, `send_voice`, or audio attachment.
4. No user-visible `COMPLETED` can be emitted from finalization alone.
5. Ordinary non-attached typed behavior and delivery remain unchanged.

## 8. Freeze recommendation

Freeze after bounded corrections; no redesign is needed. The architecture
preserves every frozen PRD authority boundary, matches the exact inspected source,
and honestly identifies the missing production arbiter/sink and quarantined
sibling output behavior. Apply the four localized corrections, make the two
specified cuts, run the contradiction scan, record new hashes, and change status
to frozen. Implementation authorization remains a separate gate.
