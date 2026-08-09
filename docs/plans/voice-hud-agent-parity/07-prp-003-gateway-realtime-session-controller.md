# PRP-003 — Gateway Realtime Session Controller

> **For Hermes:** implement with strict RED → GREEN in this isolated worktree. Keep the controller provider-neutral and keep canonical agent authority in the owning host.

**Status:** Implementation-ready — one adversarial review returned three blockers; the concrete TUI host seam, interrupt admission barrier, and attachment/transport-generation reconnect model below incorporate them.

**Goal:** Add one backend-owned realtime voice controller that opens and supervises a registered provider session, admits only authorized final operator transcripts through PRP-002, and schedules them onto the owning Hermes session's existing canonical turn path.

**Architecture:** `GatewayRealtimeVoiceController` owns provider/audio/event lifecycle and one immutable `RealtimeSessionBinding`. `TuiRealtimeTurnHost` owns opaque consume-once permits and the atomic validate → consume → canonical-enqueue linearization point against the existing Desktop/TUI `prompt.submit` handler. The controller never executes provider tool calls, calls `AIAgent`, writes transcripts, or creates a second Hermes session.

**Tech stack:** Python 3.11+, asyncio, existing realtime provider API v2, PRP-002 admission, existing host turn submission/lease path, pytest/pytest-asyncio.

---

## Dependencies and source anchors

- `agent/realtime_voice_provider.py`: API-v2 provider/session/events and optional interruption/resumption capabilities.
- `agent/realtime_voice_orchestrator.py`: current run-only provider resolution and event pump; extend only if retaining an opened session cannot reuse it without duplicated registry/capability logic.
- `agent/realtime_voice_admission.py`: immutable binding, replay ledger, opaque authorizer/ingress protocols, cancellation-safe cleanup.
- `gateway/run.py:_handle_message`: messaging host's canonical authorization/session/agent path.
- `gateway/run.py:_handle_message_with_agent`: resolves the durable session before `SessionTurnLeaseRegistry.acquire()` and then loads/runs/flushes.
- `gateway/turn_lease.py`: canonical per-durable-session serialization primitive.
- `tui_gateway/server.py:_run_prompt_submit`: Desktop/TUI canonical prompt path; actual Desktop RPC exposure remains PRP-005.
- `docs/plans/voice-hud-agent-parity/02-normative-spec.md`: VH-012–016 and gateway portions of VH-017/VH-021.
- `docs/plans/voice-hud-agent-parity/06-prp-002-provider-neutral-final-transcript-admission.md`: exact PRP-003 handoff.

Baseline at `ee8edcd8f03b35baa0d36dc75aee9f1f4cd8000e`: the four realtime suites pass **159 tests**.

## Hard scope

Production:

- Create `gateway/realtime_voice_controller.py`.
- Create `tui_gateway/realtime_voice_host.py` as the one concrete canonical-host bridge.
- Modify `agent/realtime_voice_orchestrator.py` only if required to expose one provider-resolution/open primitive reused by both `run()` and the controller.

Tests:

- Create `tests/gateway/test_realtime_voice_controller.py`.
- Create `tests/tui_gateway/test_realtime_voice_host.py`.
- Modify `tests/agent/test_realtime_voice_orchestrator.py` only with the matching shared-open contract tests.

Evidence:

- This PRP.

No renderer/Electron RPC, Talk code, provider adapter, setup/config UI, persistence schema, messaging `gateway/run.py` edit, tool executor, model tool schema, or audio-device implementation is in scope. The TUI bridge calls existing installed server handlers; it does not copy `prompt.submit` or `session.interrupt` logic.

## Trusted contracts

### Concrete TUI host attachment

`TuiRealtimeTurnHost` captures, from the already-authenticated Desktop/TUI connection:

1. immutable `RealtimeSessionBinding` fields for active profile, runtime SID, stored/durable session key, host-generated provider-session identity, routing target, and attachment selection generation;
2. the exact live session record object and transport/principal capability that opened the attachment;
3. the installed `server._methods["prompt.submit"]` and `server._methods["session.interrupt"]` handlers;
4. an async lifecycle/event sink.

Every permit operation re-reads `server._sessions[runtime_sid]` under its `history_lock` and requires object identity, durable key, profile, transport/principal, and attachment generation to remain exact. Replacement/removal of the runtime session record, transport reconnect without an explicit attachment-generation rebind, profile switch, durable-session rotation, or selection drift fails closed. Provider events and `provider_data` never supply any trusted value.

### Atomic permit and prompt submission

`TuiRealtimeTurnHost` implements both PRP-002 protocols under one controller-loop `asyncio.Lock`:

- `authorize()` validates the exact captured TUI session/transport/principal and requested binding, creates a private identity-keyed permit, and binds it to the exact immutable binding and utterance.
- `revoke()` removes that exact permit under the same lock; repeated/unknown permits are harmless no-ops.
- `submit()` under the same lock revalidates the exact TUI attachment, validates exact permit object/binding/utterance ownership, consumes the permit terminally, and synchronously invokes the existing `prompt.submit` handler with the captured runtime SID, transcript text, and `queued=True` before releasing the lock.
- A receipt is returned only when `prompt.submit` positively reports a streaming or queued claim. Any RPC error, missing/changed session, capacity rejection, or ambiguous result fails visibly; it is never converted into acceptance.
- Binding drift, wrong host, wrong utterance, reused permit, enqueue exception, cancellation, and close fail closed. A consumed permit never reappears.
- Once `prompt.submit` returns a positive claim, accepted canonical work is no longer controller-owned and survives controller close.

The bridge exposes `interrupt_and_wait(binding, timeout)` by calling the existing `session.interrupt` handler, then observing the exact captured session under `history_lock` until `running` is false. The wait is bounded and fails visibly; it never treats the interrupt acknowledgement itself as persistence/release completion.

## Controller API and lifecycle

Provide typed equivalents of:

```python
await controller.open(provider_name, setup, binding, required_capabilities=...)
controller.feed_audio(data, mime_type=...)
await controller.interrupt()
await controller.resume(...)
await controller.close(reason=...)
```

Lifecycle is deterministic and monotonic per attachment:

`disconnected → connecting → ready/listening → queued/thinking/acting/speaking → closing → closed`

`failed` and `reconnecting` are explicit branches. Every emitted controller event has a monotonic sequence number and immutable binding identity. Exactly one terminal `closed` event is emitted.

Rules:

- `open()` accepts one attachment and starts one provider session/event pump.
- Host-generated `binding.provider_session_id` is authoritative. `SessionReady.session_id` is diagnostic correlation only.
- Only `InputTranscript` reaches `FinalTranscriptAdmission`; all other normalized events are projected but cannot authorize or execute work.
- `ToolCall` and `ToolCallCancelled` are diagnostic/provisional in this slice and never dispatch tools or send fabricated results.
- Final transcript `SUBMITTED` moves through queued/thinking based on host receipt/projections, not provider acknowledgements. `agent.completed` is not emitted until the canonical host reports finalization.
- `feed_audio()` copies bytes into a bounded queue with a non-blocking admission decision. Overflow emits visible failure/backpressure and drops the rejected chunk; provider event consumption remains live.
- Queue bounds and identifier/text bounds are positive finite construction inputs with safe defaults.
- Attachment identity and provider transport generation are distinct. The immutable attachment/provider-session binding owns the admission ledger; each event-pump start captures a monotonic transport generation and stale-generation callbacks are discarded.
- A `SessionFailure` is recoverable only when the retained session advertises `SESSION_RESUMPTION` and a normalized `SessionReady.session_id` transport-resume token was observed. The controller enters `reconnecting`, keeps the same provider object/admission ledger, and accepts only explicit bounded `resume()`.
- `resume()` invokes `session.resume_session()` on the retained provider object, increments transport generation, and starts a fresh fenced event pump. Resume failure, timeout, `SessionClosed`, stream exhaustion without a recoverable failure, or a new authoritative provider-session identity is terminal and destroys the old admission ledger. Opening a replacement provider object requires a new controller attachment.
- Binding drift stops new admission immediately, revokes owned permits, and closes the attachment. It never retargets the controller.

## Interrupt and teardown

- Controller event admission and `interrupt()` share one admission barrier plus a synchronous interrupt-generation fence. `interrupt()` marks the fence before its first await, acquires the barrier, stops/truncates provider output when supported, invokes host `interrupt_and_wait(binding)`, and holds the barrier until the exact TUI turn reports `running=False` or the bounded wait fails.
- A final transcript already linearized before the fence remains the prior accepted turn. A final arriving after the fence waits at the barrier and cannot authorize or enqueue before release. Add a controlled event-barrier test proving no permit or `prompt.submit` call occurs while host release is blocked.
- Replacement speech becomes a new serialized turn only after that release; TUI `prompt.submit` locking/queueing remains the final backstop.
- `close()` first closes admission, then stops accepting audio/events, resolves worker/event tasks without self-await, closes the provider session idempotently, and emits one terminal event.
- Caller cancellation cannot orphan the provider session, event pump, queued audio, or unconsumed permit. Cleanup failures remain visible and retryable.
- `SessionClosed`, terminal stream exhaustion/failure, explicit close, repeated close, close-from-event-pump, and cancellation races converge on the same close ownership. A recoverable resumable `SessionFailure` remains nonterminal in `reconnecting` as defined above.
- Closing voice never cancels already accepted canonical work or unrelated background work.

## TDD sequence

### Task 1 — Atomic host permit and canonical enqueue

1. RED: exact binding + exact utterance permit is consumed once and schedules one callback.
2. RED: wrong/reused/foreign permit, utterance drift, every binding dimension drift, authorization denial, and enqueue failure fail closed.
3. GREEN: implement the private permit ledger and one-lock linearization.

### Task 2 — Open and normalized event pump

1. RED: known capable provider opens once, emits connecting/ready, and projects events in order.
2. RED: unknown/unavailable/missing-capability/open failure and event-stream failure are visible and leak-free.
3. GREEN: reuse provider resolution/open logic and start one supervised pump.

### Task 3 — Final transcript admission

1. RED: only final authenticated operator transcript schedules one canonical callback; partial/participant/provider-tool/duplicate/stale input never does.
2. RED: binding drift at authorize and atomic submit fails closed.
3. GREEN: create one `FinalTranscriptAdmission` from the attachment binding and route only `InputTranscript` through it.

### Task 4 — Bounded audio, interrupt, and reconnect

1. RED: bounded queue overflow is visible and does not block event consumption.
2. RED: interrupt orders provider stop before host interrupt/release.
3. RED: recoverable failure plus retained provider object and same authoritative attachment retains replay history across a new transport generation; stale old-generation events are ignored; terminal failure/new identity closes the ledger.
4. GREEN: add bounded worker and capability-gated interrupt/resume.

### Task 5 — Cancellation-safe close

1. RED: explicit/provider-terminal/error/repeated/cancelled close emits one terminal event, closes all owned resources, revokes unconsumed permits, and preserves accepted canonical work.
2. RED: close from inside the event pump cannot self-await/deadlock.
3. GREEN: implement one shielded, retryable close owner.

### Task 6 — Canonical-path conformance canary

Use a fake API-v2 provider, real `TuiRealtimeTurnHost`, installed TUI handlers, and temporary `HERMES_HOME`. Prove:

1. one final operator transcript reaches the already-selected durable session once;
2. the existing `prompt.submit` path positively claims streaming/queued work under the captured TUI session `history_lock`;
3. duplicate, participant, stale, and provider-tool events do not enter it;
4. accepted work survives controller close;
5. no second Hermes runtime, durable session, gateway, or tool executor is created.

The canary must inspect the same runtime SID and stored/durable key before and after, observe canonical completion/persistence, and prove no second TUI/gateway process or session record. A fake callback or bare list is not deployment proof.

## Verification commands

```bash
python -m pytest tests/gateway/test_realtime_voice_controller.py -q
python -m pytest tests/tui_gateway/test_realtime_voice_host.py -q
python -m pytest tests/agent/test_realtime_voice_orchestrator.py tests/agent/test_realtime_voice_admission.py tests/agent/test_realtime_voice_provider.py tests/agent/test_realtime_voice_registry.py -q
python -m pytest tests/gateway/test_turn_lease.py tests/gateway/test_realtime_voice_controller.py -q
ruff check gateway/realtime_voice_controller.py tui_gateway/realtime_voice_host.py agent/realtime_voice_orchestrator.py tests/gateway/test_realtime_voice_controller.py tests/tui_gateway/test_realtime_voice_host.py tests/agent/test_realtime_voice_orchestrator.py
git diff --check
```

Run exact commands through the isolated project environment and record actual counts.

## Acceptance criteria

- [ ] Typed open/feed/interrupt/resume/close lifecycle exists with bounded queues and one terminal event.
- [ ] Host identity includes all PRP-002 binding dimensions and never derives authority from provider fields.
- [ ] Opaque permits are exact-binding/exact-utterance, consume-once, and atomic with canonical enqueue/revocation.
- [ ] Only final authenticated operator transcripts enter admission/canonical work.
- [ ] Reconnect ledger retention occurs only for the same authoritative provider session.
- [ ] Barge-in holds an admission barrier from output stop through exact TUI `running=False`; racing final input cannot enqueue early.
- [ ] Teardown is idempotent, cancellation-safe, leak-free, and preserves accepted/background work.
- [ ] Provider tool events never execute tools.
- [ ] Focused, realtime regression, and turn-lease suites pass; Ruff and diff checks pass.
- [ ] Independent final security/concurrency review passes.
- [ ] Fake-provider + real-TUI-host canary proves same-session submission/completion without another gateway/session/executor.

## Deployment/backout

This slice has no data migration and is dormant until a surface/provider uses it. Backout is removal of the controller and optional shared-open helper. Do not replace the live Hermes install or advertise native realtime until the canonical-path canary is real and the consuming adapter/surface is capability-gated. Existing turn-based Quick Entry remains the fallback.
