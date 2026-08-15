---
title: Hermes Native Realtime HUD — Intent/Architecture Reconciliation
status: aligned-and-frozen-after-post-pr-final-review
date: 2026-08-11
intent: 01-intent-prd.md
architecture: 05-companion-architecture.md
---

# Hermes Native Realtime HUD — Intent/Architecture Reconciliation

> **Posture:** This document checks the companion architecture against the frozen
> Intent PRD. It is not an implementation receipt, architecture freeze, merge
> approval, activation authorization, restart authorization, or live-canary
> authorization.

## 1. Result

**Result: `ALIGNED_AND_FROZEN`**

The architecture preserves all frozen product-authority boundaries and provides
one explicit owner, contract, failure rule, and evidence source for every MVP
promise. No frozen product requirement is weakened or reassigned.

The architecture received an independent Fable architecture/security/concurrency
review, incorporated all four bounded corrections and both scope cuts, reconciled
PR #65845 as a reference-only implementation, and passed a final post-PR delta
review with verdict `FREEZE`. Implementation authorization remains separate.

## 2. Non-negotiable alignment summary

| Frozen intent | Architecture decision | Result |
|---|---|---|
| Voice is transport, not a second agent | Realtime provider gets audio, render text, and correlation metadata only | aligned |
| Canonical Hermes is sole reasoning/tool authority | Final transcript enters normal Gateway turn; provider receives no tools | aligned |
| Exact session and operator identity | Host-minted consume-once attachment binds route, durable session, principal, surface, and generation | aligned |
| Same capability as typed input | Voice creates a normal `MessageEvent`; no prompt/tool/model override is allowed | aligned |
| Exact persisted final response is spoken | Host resolves response text from persisted assistant row and issues digest-bound request | aligned |
| Provider must not invent another answer | Automatic response and tools are disabled; bounded pre-play transcript fidelity gate rejects mismatch | aligned |
| Native output, no silent generic fallback | Attached turn is fenced at admission before streaming TTS, then reserved at finalization before remaining generic routing; native failure is visible and terminal | aligned |
| Exact playback truth | Adapter-owned lease issues identity-bound normal-drain or interruption receipt | aligned |
| Natural barge-in | Host fences exact response and lease; replacement final is pinned and admitted once | aligned |
| Background work survives surface closure | Surface owns transport tasks only; Hermes retains canonical/delegated work | aligned |
| Existing approvals remain authoritative | Provider cannot see or resolve approvals; existing visual/text ceremony stays in force | aligned |
| Truthful HUD state | Host evidence reducer owns projection; user `COMPLETED` occurs only after drain | aligned |
| Privacy and least disclosure | Provider payload is bounded; no history, credentials, tools, memory corpus, or approval state | aligned |
| Reversible activation | Exact-SHA manifest, fresh-process activation, post-canary rollback, and leak proof are required | aligned |

## 3. Detailed requirement traceability

### 3.1 Entry and attachment

- **Intent 8.1 / Promise 1 / Success 1**
  - Architecture: `RealtimeVoiceAttachmentFactory` is captured only during an
    authenticated contextual command and consumed once.
  - Evidence: binding contains exact route, durable session, principal, surface,
    and route generation; open fails on drift.

- **Intent 8.1 manual first entry**
  - Architecture: Discord `/talk core join` remains the bounded proof entry.
  - Mute/detach: host revokes exact-generation admission before capture/queue
    cleanup; unmute creates a fresh generation and stale callbacks stay fenced.
  - Deferred: wake phrase and ambient listening are absent.

### 3.2 Transcript admission

- **Intent 8.2 / Success 6 and 11**
  - Architecture: adapter-authenticated speaker identity gates PCM; final
    transcript uses bounded item/generation replay identity; partials remain
    projection-only; one consume-once permit reaches the canonical route.
  - Negative evidence: unauthorized, malformed, stale, replayed, duplicated, and
    wrong-generation events create no user row and no turn lease.

- **Intent 8.5 in-flight replacement**
  - Architecture: one bounded `PinnedUtterance` slot captures the first authorized
    final while a turn or delivery is active; it never enters the in-flight turn.
  - Ordering: once pinned, the correction reserves the next canonical user-turn
    position ahead of typed input arriving later, but never jumps ahead of a turn
    that already owns the canonical lease.
  - Overflow: additional final transcripts fail visibly rather than overwrite.

### 3.3 Canonical reasoning and capabilities

- **Intent 8.3 / Authority 9 / Gate A**
  - Architecture: voice synthesizes a normal `MessageEvent` and invokes the same
    Gateway handler and durable session entry as typed input.
  - Provider tools: empty and rejected on setup or event receipt.
  - No alternate model, prompt, tool registry, skill list, memory, or approval
    state is supplied by the voice lane.

### 3.4 Native output

- **Intent 8.4 / Success 7 and 8 / Gate B**
  - Architecture: canonical finalization returns a consume-once
    `CanonicalTurnReceipt`; native delivery reserves before generic voice paths;
    host resolves exact persisted assistant text and digest; provider renders
    with no tools and no automatic response; adapter plays bounded PCM.
  - Fidelity: provider transcript must match the canonical text before PCM is
    eligible for playback.
  - Failure: mismatch, missing transcript, spool overflow, wrong metadata, or
    provider error emits visible native failure with no generic fallback.

### 3.5 Completion

- **Intent 8.7 / Metrics 14.1**
  - Architecture splits internal `CANONICAL_FINALIZED` from user-visible
    `COMPLETED`.
  - `COMPLETED` requires canonical persistence, native response identity,
    fidelity proof, and exact-lease normal drain.
  - `INTERRUPTED` requires exact-response cancellation plus exact-lease
    interruption iff a playback lease opened; a pre-lease interruption destroys
    the spool without inventing a lease receipt. It remains mutually exclusive
    with `COMPLETED`.

### 3.6 Barge-in

- **Intent 8.5 / Success 9 / Gate C**
  - Architecture treats authorized-provider speech-start as timing input only;
    the host chooses the exact response and lease to interrupt.
  - Replacement final remains pinned until the old delivery barrier is terminal,
    then is admitted exactly once.
  - Session state and unrelated Hermes work are not interruption targets.

### 3.7 Approvals and background work

- **Intent 8.6 / Promises 4 and 9 / Success 5 and 10**
  - Architecture keeps approval records and resolution entirely in Hermes.
  - Closing capture, provider, queues, spools, and leases cannot call canonical
    task/delegation cancellation merely because the surface ended.
  - Process-local delegated work is not claimed to survive host restart.

### 3.8 Privacy

- **Intent 9.1 / Success 11**
  - Architecture sends the provider only authorized audio, canonical render text,
    non-secret correlation identity, model/voice, and bounded transport settings.
  - No credentials, provider tokens, tools, approval records, history, memory,
    prompts, or unrelated context cross the provider boundary.
  - Canary payloads are non-sensitive; unauthorized listener/speaker negatives
    are mandatory.

### 3.9 Failure, cleanup, and rollback

- **Intent Success 10, 12, and 14 / Gate E**
  - Architecture defines a surface-owned task/resource ledger and zero-resource
    close invariant.
  - Native failures do not fall back silently.
  - Activation uses an exact-SHA external manifest and fresh process.
  - Post-canary rollback restores the prior reviewed mapping in another fresh
    process and proves ordinary typed behavior plus zero realtime resources.

### 3.10 Exact minimum canary operations

- **Intent Success 4 / Minimum demo 3**
  - Architecture: canonical Hermes executes
    `skills_list(category="autonomous-ai-agents")` and verifies `hermes-agent`
    appears; provider sees no tool schema or call.
- **Intent Success 5 / Minimum demo 9**
  - Architecture: canonical Hermes launches a bounded no-tool `delegate_task`
    returning exactly `native-hud-delegate-ready`; its process-local ownership
    remains observable after transport close/barge-in with no restart-survival
    claim.
- Neither operation requires live approval; deterministic approval lifecycle
  tests remain mandatory and provider speech cannot mint/resolve approval.

## 4. Current-source reconciliation

| Current source state | Architectural interpretation |
|---|---|
| Agent canonical attachment/input branch | Valid Gate A foundation; not full native output |
| Agent native output-controller branch | Valid contract prototype; not production-wired |
| Agent Discord generic-output sibling | `86c756de` can activate generic auto-TTS through `MessageType.VOICE`; it is not native provenance or a dependency of the native controller |
| Talk main input-only provider registration | Reusable wire/auth foundation; legacy join remains non-parity |
| Talk `feat/discord-core-session-adapter` | Valid capture-only canonical attachment proof; not duplex native output |
| Agent PR #65845 AG-UI adapter | Useful reference for event terminality, fail-closed resume races, CAS cleanup, config, and auth; not Talk ingress because it reconstructs client history and creates a fresh `AIAgent` per run |
| No integrated reviewed stack | Architecture remains draft and implementation blocked |

The architecture does not reinterpret any one branch as deployed or complete.
Every target behavior still requires one exact candidate stack and cross-repo
review evidence.

## 5. Architecture-discovered corrections to current prototypes

These are implementation corrections, not PRD changes:

1. Current controller prototypes expose canonical `COMPLETED` before native drain.
   Target architecture replaces this with internal `CANONICAL_FINALIZED`; only the
   evidence reducer may emit user-visible `COMPLETED` after normal drain.
2. Current native output prototypes can begin playback before output-transcript
   fidelity is proven. Target architecture inserts a bounded pre-play spool and
   exact canonical-text fidelity gate.
3. Current Talk core provider is input-only. It must implement explicit response,
   response cancellation, normalized output events, and strict output/tool
   rejection without restoring provider-owned conversation authority.
4. Current core-session bridge is idle-only. Target architecture adds one bounded
   replacement slot and serialized post-terminal admission.
5. Current output sink is a protocol without an exact Discord production owner.
   Target architecture makes the captured Discord adapter mint and validate the
   exact playback lease.
6. Current canonical finalization receipt is not yet integrated with an atomic
   native-output seam. Target architecture adds an admission-time fence before
   streaming-TTS setup and a finalization-time reservation before remaining
   generic voice/TTS/attachment paths.
7. PR #65845's client-history/fresh-agent and resume-monkeypatch techniques do not
   attach to the exact durable Talk session. The target reuses only its proven
   lifecycle/race patterns behind the existing host-minted attachment boundary.

## 6. Deferred product decisions remain deferred

The architecture does not decide:

- spoken progress;
- transcript mirroring policy;
- latency pass/fail budgets;
- background speech after visual focus loss;
- spoken approval;
- wake phrase;
- additional providers.

The interfaces leave extension points, but no deferred feature exists in the MVP
state machine or canary acceptance path.

## 7. Contradiction scan

The draft architecture was checked for the following prohibited reversals:

- provider tools or provider-owned tool execution: **absent**;
- copied Hermes prompt/history/memory/tool registry: **absent**;
- client-supplied history or fresh surface-owned agent as Talk authority:
  **forbidden**;
- provider automatic responses: **forbidden**;
- second canonical response: **forbidden**;
- generic TTS/audio-attachment fallback: **forbidden**;
- completion before playback drain: **forbidden**;
- interruption cancelling canonical session/background work: **forbidden**;
- transcript/provider metadata as identity: **forbidden**;
- voice-only approvals in MVP: **absent**;
- live canary or activation authorization from planning artifacts: **absent**.

## 8. Final post-PR review questions

The pre-PR Fable review found no blocker and four bounded corrections. After the
PR #65845 assessment, a final delta review must challenge the corrected boundary:

1. Is bounded pre-play transcript fidelity sufficient evidence that provider PCM
   renders the exact canonical text, and are mismatch/overflow semantics safely
   fail-closed?
2. Can the Discord adapter prove local player-drain without conflating encoder
   consumption with human audibility? The architecture says yes only for
   adapter/device drain; audibility remains a human canary observation.
3. Does the one-slot pinned replacement policy behave deterministically across
   speech-start, transcript-final, provider-complete, and close races?
4. Can native reservation linearize before every generic voice/TTS/attachment
   path without changing ordinary typed/text behavior?
5. Does controller/provider close retain enough ownership to clean every late
   event, task, spool, queue, and playback lease exactly once?
6. Can output transcript comparison be byte-exact across provider punctuation,
   whitespace, and Unicode behavior? If not, the provider is not qualified for
   the frozen “exact canonical response” MVP without a stronger API guarantee.
7. Is the five-capability Talk-first host seam genuinely sufficient without
   importing AG-UI protocol, client history, tool registration, or run registries?

## 9. Review and freeze gate

The gate passed:

1. initial Fable review: `APPROVE_WITH_CORRECTIONS`, no blockers;
2. four corrections applied in the architecture;
3. two speculative requirements removed;
4. PR #65845 inspected at exact base/head and reconciled as reference-only;
5. deterministic contradiction scan passed;
6. final post-PR Fable review: `FREEZE`, no blockers or further corrections;
7. architecture and reconciliation statuses changed explicitly to frozen.

This freeze authorizes implementation planning only. It does not authorize code,
merge, installation, restart, activation, deployment, or a live canary.
