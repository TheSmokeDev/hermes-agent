# PRP-002 — Provider-Neutral Final-Transcript Admission

> **For Hermes:** Implement task-by-task with strict RED → GREEN. This PRP received its one adversarial planning review; move further review to executable evidence and the final diff.

**Status:** Reviewed; implementation-ready
**Stack base:** `95323478b5d9cc9fe1d3db0a912466138bf47dae` (`feat/realtime-final-transcript-admission`)
**Depends on:** PRP-001 canary `92504e2ed771cc0a91b763838070589e60da22c8`; provider API v2 in `agent/realtime_voice_provider.py`
**Goal:** Admit one finalized operator transcript into an already-bound canonical Hermes session exactly once through a host-issued permit, without granting provider events tool, identity, session, or persistence authority.

## Business context

PRP-001 proved that voice can reach the normal Hermes prompt path through the existing Desktop STT controller. Native realtime providers now need the same authority seam. PRP-002 creates only that seam. PRP-003 will own gateway realtime-session lifecycle and wire this service to a live host; PRP-004 will migrate Talk's OpenAI adapter.

## Governing requirements

This slice implements the provider-native admission portions of VH-001–VH-011:

- provider metadata, transcript role, and display data never establish operator authority;
- partial transcripts never become user messages;
- one opaque host-issued permit admits one final utterance to one exact host binding;
- provider-session/turn/item replay identity is reserved before the first await and retained for the full provider-session lifetime;
- rejected, malformed, unauthorized, cancelled, closed, stale, and failed attempts never re-arm;
- canonical submission atomically revalidates and consumes the permit before enqueueing through the normal serialized same-session turn path;
- this module never calls `AIAgent.run_conversation`, dispatches a tool, writes transcript rows, or creates a session.

## Hard scope

**Create only:**

- `agent/realtime_voice_admission.py`
- `tests/agent/test_realtime_voice_admission.py`
- this PRP

**Do not modify:**

- `agent/realtime_voice_provider.py`
- `agent/realtime_voice_orchestrator.py`
- `gateway/**`
- `tui_gateway/**`
- Desktop/Electron files
- Hermes Talk
- provider adapters or registries

## Current anchors

- `agent/realtime_voice_provider.py:179` defines immutable `InputTranscript(item_id, turn_id, text, final, role, provenance, provider_data)` and validates role/provenance consistency.
- `InputTranscript` intentionally lacks `provider_session_id`; it SHALL NOT be recovered from `provider_data`.
- `agent/realtime_voice_orchestrator.py:45-48` forwards normalized events to a host without transferring authority.
- `gateway/turn_lease.py` already serializes canonical turns by resolved durable session ID. PRP-002 targets an injected ingress that reuses that path; it must not duplicate the lease.
- No canonical `AgentTurnReceipt` type exists. PRP-002 therefore treats the host receipt as an opaque generic and makes no durable-finalization claims.

## Typed contract

`agent/realtime_voice_admission.py` SHALL define frozen, validated records and generic protocols equivalent to:

```python
PermitT = TypeVar("PermitT")
ReceiptT = TypeVar("ReceiptT")

@dataclass(frozen=True, slots=True)
class RealtimeSessionBinding:
    profile_id: str
    routing_key: str
    runtime_session_id: str
    durable_session_id: str
    provider_session_id: str
    selection_generation: int

@dataclass(frozen=True, slots=True)
class RealtimeUtterance:
    provider_session_id: str
    provider_turn_id: str
    item_id: str
    text: str
    received_at: float

class RealtimeInputAuthorizer(Protocol[PermitT]):
    async def authorize(
        self,
        binding: RealtimeSessionBinding,
        utterance: RealtimeUtterance,
    ) -> PermitT | None: ...

    async def revoke(self, permit: PermitT) -> None: ...

class SameSessionTurnIngress(Protocol[PermitT, ReceiptT]):
    async def submit(
        self,
        binding: RealtimeSessionBinding,
        utterance: RealtimeUtterance,
        permit: PermitT,
    ) -> ReceiptT: ...
```

`SameSessionTurnIngress.submit` SHALL atomically validate the current principal/profile/routing/runtime/durable/provider binding, consume the permit once, and enqueue through canonical serialized ingress. `RealtimeInputAuthorizer.revoke` SHALL be idempotent and race safely at the same host-owned linearization point. If close/drift revocation wins, submit fails closed; if submit wins, later revoke is a no-op and accepted Hermes/background work survives voice close.

The concrete service SHALL be `FinalTranscriptAdmission[PermitT, ReceiptT]`, bound at construction to one trusted `RealtimeSessionBinding`, authorizer, ingress, fixed replay capacity, maximum final-transcript characters, and injectable monotonic clock. The bound `provider_session_id` is supplied by the trusted host/controller; provider data is never consulted.

`admit(event: RealtimeVoiceEvent)` returns `AdmissionResult[ReceiptT]` with one compact `AdmissionStatus` and an optional opaque receipt:

- `IGNORED_PARTIAL`
- `SUBMITTED`
- `REJECTED`
- `DUPLICATE`
- `CAPACITY_EXHAUSTED`
- `CLOSED`

Detailed authorization failure data remains host-side and is never exposed to the provider.

## State and linearization

1. Non-`InputTranscript` events return `REJECTED` without authorizer/ingress calls.
2. A partial input returns `IGNORED_PARTIAL` and does not reserve its final replay key.
3. A final key is `(bound provider_session_id, event.turn_id, event.item_id)`.
4. Under an internal async lock, every final checks closed/capacity/replay state and reserves the key **before the first await**. A duplicate with changed text or role remains duplicate.
5. The replay ledger is fixed-capacity and never evicts during the provider-session lifetime. Once a new final would exceed capacity, the service latches `CAPACITY_EXHAUSTED` and rejects every unseen final for the rest of that provider session. Existing keys remain duplicates.
6. PRP-003 SHALL preserve the same admission object/ledger across reconnect or resume of the same provider session. Only a proven terminal provider-session boundary discards it; old-generation callbacks remain closed/suppressed.
7. Final participant input and non-string, blank, whitespace-only, or over-limit text remain terminally reserved and return `REJECTED` without authorization.
8. Accepted text is trimmed once. `provider_data` never enters binding, utterance, permit, receipt, or status.
9. Authorization happens only after core gates. `None`, exceptions, and cancellation are terminal for the key.
10. A returned permit is registered as pending under the state lock before any later await. Every issued permit has exactly one terminal ownership outcome: ingress atomically consumes it, or the service performs idempotent shielded revocation.
11. After every await, the service rechecks closed/generation state before continuing. Close linearizes closed state first, collects pending permits under lock, then revokes outside the lock.
12. Close racing submit is decided only by the host's shared atomic submit/revoke point. The admission service never claims completion itself.
13. Exceptions remain visible to PRP-003, but the key stays reserved. No retry of the same provider identity can replay.
14. Binding identifiers are nonblank, trimmed, and bounded to `MAX_IDENTIFIER_LENGTH`. Generation, replay capacity, and max transcript characters are positive non-boolean integers. Default replay capacity is 1024; default maximum transcript length is 32,768 characters.

## Non-goals

- No provider open/feed/interrupt/close API (PRP-003).
- No Talk/OpenAI adapter migration (PRP-004).
- No realtime Desktop audio/HUD state (PRP-005).
- No participant-to-operator mapping, Discord adapter, tools, approvals, screenshots, or streaming.
- No durable replay journal. The in-memory ledger is provider-session-scoped; PRP-003 owns lifecycle/resume preservation and PRP-009/010 own recovery/release proof.

## Strict RED → GREEN tasks

### Task 1 — Final-only authorized tracer

Create `tests/agent/test_realtime_voice_admission.py` first.

**RED:**

- `test_partial_operator_transcript_is_display_only_and_does_not_reserve_final_key`
- `test_authorized_final_operator_submits_exact_utterance_once`

Run:

```bash
./.venv/Scripts/python.exe -m pytest tests/agent/test_realtime_voice_admission.py -q
```

Expected RED: `agent.realtime_voice_admission` missing.

**GREEN:** Add the minimum records, generic protocols, statuses, result, and service needed for those tests.

### Task 2 — Authority and malformed-input gates

Add one RED → GREEN behavior at a time:

- operator role alone cannot admit without a host permit;
- participant input and provider-data identity spoof never call authorizer/ingress;
- rejected final cannot be relabelled/replayed as operator;
- duplicate identity with changed text remains duplicate;
- non-string, blank, and over-limit final text are terminal rejections;
- invalid binding identifiers/generation/capacity inputs fail construction;
- tool calls and all non-input events never reach authorizer/ingress.

### Task 3 — Replay, concurrency, cancellation, and close

Add one RED → GREEN behavior at a time using `asyncio.Event` barriers, never sleeps:

- concurrent duplicate finals call authorize/submit once;
- authorizer `None`, exception, or cancellation leaves identity terminal;
- close while authorization is pending revokes a late permit and never submits;
- close racing submit obeys atomic consume-or-revoke behavior;
- reused permit object cannot submit two utterances (host fake proves atomic consumption);
- submit cancellation never reopens identity or creates a late completion claim;
- fixed replay capacity fails closed without evicting old identities;
- same turn/item in distinct provider sessions are distinct admission objects;
- resumed same provider session retains the same replay history;
- close is idempotent and suppresses late callbacks.

### Task 4 — Regression and quality gates

```bash
./.venv/Scripts/python.exe -m pytest tests/agent/test_realtime_voice_admission.py -q
./.venv/Scripts/python.exe -m pytest \
  tests/agent/test_realtime_voice_admission.py \
  tests/agent/test_realtime_voice_provider.py \
  tests/agent/test_realtime_voice_orchestrator.py \
  tests/agent/test_realtime_voice_registry.py -q
./.venv/Scripts/ruff.exe check \
  agent/realtime_voice_admission.py \
  tests/agent/test_realtime_voice_admission.py
git diff --check
```

## Acceptance criteria

- [ ] Only a finalized operator transcript can request a host permit.
- [ ] One provider-session/turn/item identity reaches canonical ingress at most once.
- [ ] Replay history is retained without eviction for the live/resumed provider session; capacity exhaustion latches fail-closed.
- [ ] Participant, partial, malformed/blank/over-limit, denied, stale/closed, cancelled, and exceptional attempts never submit.
- [ ] Provider session identity comes only from the trusted bound host, never provider data.
- [ ] Permit submission/revocation share a host-owned atomic linearization point; every issued permit is consumed or cancellation-safely revoked.
- [ ] Close during authorization revokes a late permit and blocks submission; accepted canonical work survives later voice close.
- [ ] Provider metadata never becomes identity or authority.
- [ ] No direct tool dispatch, `run_conversation`, persistence write, gateway session creation, provider-specific behavior, or invented durable-completion receipt is introduced.
- [ ] Focused/regression tests, Ruff, and diff checks pass with exact receipts.

## Adversarial planning review disposition

Independent review against revision `92504e2ed771cc0a91b763838070589e60da22c8` returned four blockers; all are incorporated above:

1. marker permits were replaced by opaque generic host permits with atomic submit/revoke semantics;
2. LRU eviction was removed; capacity now latches fail-closed for the provider-session lifetime and resume must retain the ledger;
3. provider session identity is trusted constructor binding only, never `provider_data`;
4. the nonexistent `AgentTurnReceipt` was replaced by an opaque generic receipt with no premature completion claims.

The review also confirmed that the provider event and orchestrator boundaries are already suitable and SHALL remain unchanged in PRP-002.

## Backout

Revert the new standalone module/tests and this PRP. No migration, configuration, provider registration, live gateway state, or persisted session data is changed. PRP-001 turn-based voice remains intact.

## Completion evidence

The implementation handoff SHALL include branch/revision, changed files, every acceptance criterion mapped to a test, exact RED/GREEN/gate commands and exit codes, final security/quality review dispositions, and confirmation that no push/PR/merge occurred without separate authorization.
