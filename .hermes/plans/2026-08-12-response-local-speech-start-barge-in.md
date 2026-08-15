# Response-Local Speech-Start Barge-In Implementation Plan

> **For Hermes:** Execute task-by-task with strict vertical RED → GREEN TDD. One writer; one adversarial plan review; no installation or activation until code review closes.

**Goal:** When the operator starts speaking during one native response, stop only that exact provider response and Discord playback lease, retain at most one bounded replacement transcript, and admit it into the same canonical Hermes session only after provider, playback, and host barriers converge.

**Architecture:** `InputSpeechStarted` remains a passive Talk event. The Agent controller synchronously fences current output, installs one response-local barrier, then returns immediately to the single provider event pump. A retained worker shares the existing native terminal owner and joins exact provider cancellation/terminal, validated lease interruption, and canonical host interruption before releasing one buffered final transcript through the existing admission path.

**Reference patterns:** Preserve Hermes built-in full-duplex ordering—latch before cut, capture through interruption, reject non-authoritative/echo-like input—and Pipecat/Dograh interruption concepts where compatible. Do not inherit global queue clearing, provider-agent authority, automatic provider responses, or broad task cancellation.

**Files:**
- Modify: `gateway/realtime_voice_controller.py`
- Modify: `tests/gateway/test_realtime_voice_controller.py`
- Read-only dependency: Talk `6c3ed3a24cb87b2b493ba5b7faca8d116f9d5bd1`

---

## Task 1: Nonblocking speech-start fence

1. Add a deterministic pump-level test proving `InputSpeechStarted` returns before blocked provider cancellation, lease interruption, or host interruption.
2. Run the exact test and capture valid RED.
3. Add one `_BargeInBarrier` bound to the exact native response object, transport generation, interrupt generation, speech item, terminal gates, one transcript slot, and one retained worker.
4. Handle exact `InputSpeechStarted` before transcripts: under `_native_lock`, latch/fence once, upgrade native terminal intent to `interrupt`, retain the worker, and return to the pump.
5. Make duplicate starts idempotent; no active native response remains passive.
6. Run focused GREEN and commit.

## Task 2: Keep the event pump live after provider completion

1. Add RED where provider `ResponseCompleted` arrives while local drain is blocked, followed by speech-start.
2. Change completion handling to schedule/share the retained drain owner instead of awaiting drain inside `_handle_event`.
3. Let speech-start upgrade the same owner from drain to interrupt; never create a second cleanup owner.
4. Assert no false `COMPLETED`, one exact lease terminal action, and continued pump progress.
5. Run GREEN and commit.

## Task 3: Exact provider-terminal and physical-stop barrier

1. Add vertical REDs for exact `Interruption`, completed-after-cancel race, wrong/subclass/stale terminal, and every relevant ordering with playback interruption.
2. Accept only the bound exact `(response_id, turn_id)` terminal. If provider completed before speech-start, initialize that gate satisfied.
3. Share exact `cancel_response(response_id)` and existing native terminalization; expose only a validated interrupted lease receipt to the barrier.
4. Join `host.interrupt_and_wait(...)` without blocking the event pump.
5. Release only after provider terminal + `receipt.interrupted is True` + host barrier. Revalidate against a separately retained exact barrier/attachment/state identity and generations; do not depend on `_native_response is state` after terminalization clears ordinary ownership.
6. Fail closed with sanitized lifecycle details on any timeout/failure.
7. Run GREEN and commit.

## Task 4: One bounded replacement transcript

1. Add RED for partial→final, final→partial, duplicate/conflicting final, wrong item, participant provenance, subclasses, malformed text, and UTF-8/character oversize.
2. While the barrier exists, ignore partials and retain only the first exact bounded final operator transcript matching the speech-start item.
3. Never authorize or submit while any barrier gate remains blocked.
4. After all gates converge, reacquire admission ownership, revalidate attachment/response/generations, detach the barrier, and send the buffered final through existing `FinalTranscriptAdmission.admit()` and native response-start paths.
5. Barrier completion without a final returns to listening without inventing a turn.
6. Run GREEN and commit.

## Task 5: Lifecycle, pending-start, and race closure

1. Add a deterministic RED where speech-start arrives after native request admission but before `ResponseStarted`. Prove current terminalization would clear `_native_response` too early and reject the legitimate later start.
2. Add a retained pending-start barge-in phase on the exact barrier/state. When no provider response ID or lease exists yet, do not clear native ownership and do not finish the terminal owner; wait independently for either exact `ResponseStarted` binding or definitive start failure/close while the event pump remains live.
3. Permit only the exact authenticated late `ResponseStarted` for that retained interrupted-pending state. Bind its exact response/turn, acquire/bind its exact lease, and then feed the same single cancellation/physical-stop owner. Assert no unsolicited-event failure and exactly one eventual provider cancel and lease interrupt.
4. Add deterministic event-gated REDs for speech-start racing blocked acquire/write/drain, public interrupt, close, `SessionFailure`, `SessionClosed`, and reconnect.
5. Ensure all losers share or await the one terminal/barrier owner; no self-await, duplicate cancel, duplicate lease terminal call, stale admission, or retained task.
6. On start failure, timeout, close, reconnect, or session failure, terminate the pending-start wait, fence and discard the buffered transcript, and clear authority exactly once.
7. Run controller GREEN and commit.

## Task 6: Verification and handoff

Run without a broad Agent-suite wait:

```bash
python -m pytest -q -p no:cacheprovider tests/gateway/test_realtime_voice_controller.py
python -m pytest -q -p no:cacheprovider tests/agent/test_realtime_voice_provider.py
python -m pytest -q -p no:cacheprovider \
  tests/gateway/test_realtime_voice_messaging_host.py \
  tests/gateway/test_realtime_voice_messaging_integration.py \
  tests/tui_gateway/test_realtime_voice_host_integration.py
```

Then:

- pinned Talk focused response-cancellation/speech-start tests with Agent on `PYTHONPATH`;
- Ruff on changed files;
- Python compilation;
- `git diff --check`;
- exact clean-state and SHA receipts;
- authority/spec review followed by concurrency/security review;
- fix only concrete critical/important findings under fresh RED → GREEN cycles;
- commit a durable barge-in handoff.

## Acceptance invariants

- One speech start → at most one response-local barrier.
- Provider event pump never waits on the barrier it must satisfy.
- Only exact `response.cancel(response_id)` and exact lease interruption occur.
- Provider completion alone never releases replacement admission while PCM drains.
- At most one bounded exact final replacement transcript is retained.
- Replacement admission occurs only after provider terminal, validated physical-stop receipt, and host barrier.
- Canonical Hermes remains the sole executor and authority.
- No generic TTS, attachment fallback, provider tool, second agent, unrelated-audio interruption, or remote-audibility claim.
