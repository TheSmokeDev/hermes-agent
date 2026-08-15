# Native Response-Local Cancellation Implementation Plan

> **For Hermes:** Use subagent-driven development and strict RED→GREEN slices. One writer per repository. Keep Agent and Talk commits separate and local.

**Status:** frozen-after-one-adversarial-review

**Goal:** Extend the existing host-authoritative explicit native-response implementation with exact provider-response cancellation, then make pinned Hermes Talk consume that additive contract and map one canonical no-tool response through exact OpenAI response/audio/cancellation lifecycle events.

**Architecture:** Canonical Hermes continues to own response text, durable identity, tools, approvals, and persistence. The Agent API exposes a provider-neutral `RESPONSE_CANCELLATION` capability and replay-safe `cancel_response(response_id)` operation. Talk translates already-validated `RealtimeResponseRequest` objects into one explicit no-tool `response.create`, binds opaque correlation to real provider response/item IDs, emits normalized output events, and sends exact response-local cancellation. Talk never executes tools or creates a second agent.

**Tech stack:** Python 3.11+, asyncio, dataclasses, OpenAI Realtime JSON over Talk's existing shared wire, pytest, Ruff.

---

## Pinned inputs

- Slice 1 approved Agent code head: `08b62a2ff88bc501d58fc983379562d765494ecc`
- Fresh Agent candidate: `C:\Users\Degen\isolated-dev\hermes-agent-native-cancel-20260811`
- Reused Agent explicit-response stack now at candidate head `817b2a9ab`:
  - static claim hardening derived from `cbc79bbea`
  - explicit response commits derived from `16e4b5717`, `be34ddb6b`, `c7def741e`, `5cf3f319e`, `08d07f445`
- Pinned Talk base: `99d8f2a3971db074bd2649c56d9afc91b63c57e2`
- Fresh Talk candidate: `C:\Users\Degen\isolated-dev\hermes-talk-native-cancel-20260811`
- Provider fidelity receipt: `PASS_FIDELITY_IDENTITY_PARTIAL`
- Final rendering request shape: `conversation:none`, audio-only, `tools:[]`, `tool_choice:none`, exact canonical text and opaque correlation.

## Hard boundaries

This slice does **not** implement Discord PCM playback, physical drain, playback leases, replacement-transcript admission, production activation, installation, restart, live cancellation proof, or a voice canary. Generic TTS remains fenced. No provider tools or autonomous responses are permitted.

## Task 1 — Agent additive response-local cancellation contract

**Files**
- Modify: `agent/realtime_voice_provider.py`
- Test: `tests/agent/test_realtime_voice_provider.py`

**RED**
1. Require `RealtimeCapability.RESPONSE_CANCELLATION` to map to protected `_cancel_response`.
2. Add normalized passive `InputSpeechStarted(item_id, audio_start_ms)` with exact bounded item ID and exact nonnegative built-in integer offset. It carries transport timing only and must never cancel output or admit canonical work.
3. Require exact built-in, nonblank, trimmed, bounded `response_id` before capability/hook dispatch.
4. Prove unsupported capability and closed/terminal sessions fail before provider I/O.
5. RED→GREEN duplicate/in-flight/capacity ordering before provider I/O.
6. RED→GREEN public waiter cancellation followed by late hook success and one tombstone.
7. RED→GREEN public waiter cancellation followed by late hook failure and one terminal failure.
8. RED→GREEN spontaneous versus intentional provider `CancelledError`.
9. RED→GREEN failed close while both explicit-send and cancellation operations overlap; pin both exact intents until each exits.
10. RED→GREEN eager success/failure publication and stale done-callback replacement safety.
11. RED→GREEN bounded non-evicting compact response-ID tombstones and zero retained state.

**GREEN**
- Add the passive speech-start event plus distinct cancellation capability, public method, protected hook, bounded in-flight task map, accepted tombstones, intentional-close overlap markers, eager-safe cleanup, and first-terminal-cause behavior parallel to explicit response sends.
- Do not overload `EXPLICIT_INTERRUPTION` or infer adapter-local active response state in the generic layer.

**Gate**
```bash
python -m pytest -q tests/agent/test_realtime_voice_provider.py
python -m ruff check agent/realtime_voice_provider.py tests/agent/test_realtime_voice_provider.py
git diff --check
```

Commit locally: `feat(realtime): add response-local cancellation contract`

## Task 2 — Talk typed setup migration and additive capability detection

**Files**
- Modify: `talk_core_realtime.py`
- Modify: `tests/test_core_realtime_adapter.py`
- Test/add compatibility coverage in `tests/test_realtime_contract.py`

**RED**
1. Reproduce the current collection failure caused by `provider_options["automatic_response"]` shadowing the new typed field.
2. Prove upgraded setup reads exact `automatic_response`, `input_audio`, and `output_audio` typed fields while the older API surface remains importable/input-only.
3. Probe explicit-response support independently from cancellation. A shim with complete explicit-output symbols but no cancellation/passive-speech additions must still advertise and support `EXPLICIT_RESPONSE`; only `RESPONSE_CANCELLATION` and passive speech mapping remain absent. A partial cancellation symbol set must advertise no cancellation without poisoning explicit output or the input-only baseline.

**GREEN**
- Migrate new-contract fixtures/provider validation to typed fields.
- Preserve standalone/older-contract behavior additively.
- Keep capabilities input-only until Tasks 3–4 provide every required hook/event path.

**Gate**
```bash
PYTHONPATH='<exact Agent candidate>' python -m pytest -q tests/test_core_realtime_adapter.py tests/test_realtime_contract.py
```

Commit locally: `fix(core): consume typed realtime setup fields`

## Task 3 — Talk explicit canonical response adapter

**Files**
- Modify: `talk_core_realtime.py`
- Modify: `talk_openai_realtime.py` only for pure core-lane explicit-response/cancellation builders; preserve legacy `encode_command()` unchanged
- Reuse without duplicating: `talk_openai_realtime.py`, `talk_wire.py`
- Test: `tests/test_core_realtime_adapter.py`
- Test: `tests/test_openai_realtime.py`

**RED slices**
1. Pure wire-builder tests prove exact `RealtimeResponseRequest` emits one proven `response.create` with opaque correlation, exact canonical text, audio-only output, exact PCM format, `tools:[]`, and `tool_choice:none`; legacy command encoding remains byte-for-byte unchanged.
2. Complete future state capacity is reserved before wire I/O; duplicate/collision precedes capacity failure.
3. `response.created` consumes exactly one pending token and binds a real provider response ID.
4. Output audio, transcript, EOS, and item identity remain exact and monotonic.
5. Recursive authority guards reject tool/function shapes before mutation.
6. Strict `response.done` accepts only the live-proven `message → output_audio` completion and requires audio, EOS, and terminal transcript.
7. Send failure wins over blocked receive EOF and clears all state.

**GREEN**
- Store compact pending/active/completed correlation state; canonical text exists only during request construction/send.
- Map exact output into Agent `ResponseStarted`, `OutputAudio`, `OutputTranscript`, and `ResponseCompleted` events.
- Advertise `EXPLICIT_RESPONSE` only after the full path is present.

Commit locally: `feat(core): render canonical responses through realtime`

**Gate**
```bash
PYTHONPATH='<exact Agent candidate>' python -m pytest -q tests/test_openai_realtime.py tests/test_core_realtime_adapter.py tests/test_realtime_contract.py
python -m ruff check talk_openai_realtime.py talk_core_realtime.py tests/test_openai_realtime.py tests/test_core_realtime_adapter.py tests/test_realtime_contract.py
git diff --check
```

## Task 4 — Talk exact response-local cancellation and terminal races

**Files**
- Modify: `talk_core_realtime.py`
- Modify: `talk_openai_realtime.py` only for the pure exact cancel builder
- Test: `tests/test_core_realtime_adapter.py`
- Test: `tests/test_openai_realtime.py`

**RED slices**
1. Unknown/completed/already-cancelling response IDs reject before wire I/O.
2. Exact active response sends exactly one command with unique opaque `event_id` and exact `response_id`; global cancellation is forbidden.
3. Wire-send failure releases only the exact reservation while the host base preserves terminal failure truth.
4. Requested `status=cancelled` validates side-effect-free before mutation: exact active response/correlation and cancellation reservation; absent/empty output when no item was bound; otherwise exactly one already-bound assistant `message` item with audio-only partial content; optional `object` only as exact built-in string `"realtime.item"`; status details absent/`None` or an exact mapping with `type:"cancelled"`, `reason` in `{"client_cancelled","turn_detected"}`, `error` absent/`None`, and no unknown fields. Direct-mapper atomicity and event-pump cleanup tests cover invalid details, wrong/new item identity, documented `object`, and recursive structured authority. Only then emit one exact `Interruption` and tombstone state.
5. Strict `status=completed` may win after cancellation and emit completion once; cancellation winning first makes later deltas/completion fail closed.
6. Unsolicited/wrong-ID/wrong-correlation/replayed/tool-bearing terminals do not mutate state.
7. Terminate/close clears pending, active, cancellation, item, transcript/audio, correlation, and tombstone state.
8. Passive `input_audio_buffer.speech_started` maps to a typed passive event only; it never cancels or admits work itself.

**GREEN**
- Implement `_cancel_response(response_id)` over Talk's shared wire.
- Emit `{type:"response.cancel", event_id:<uuid>, response_id:<exact active>}`.
- Advertise `RESPONSE_CANCELLATION` only when every additive symbol and hook exists.

Commit locally: `feat(core): cancel exact realtime responses`

**Gate**
```bash
PYTHONPATH='<exact Agent candidate>' python -m pytest -q tests/test_openai_realtime.py tests/test_core_realtime_adapter.py tests/test_realtime_contract.py
python -m ruff check talk_openai_realtime.py talk_core_realtime.py tests/test_openai_realtime.py tests/test_core_realtime_adapter.py tests/test_realtime_contract.py
git diff --check
```

## Task 5 — Cross-repository verification and immutable reviews

1. Agent focused and surrounding gateway suites against the exact cancellation commit.
2. Talk focused/full suite with exact Agent candidate first on `PYTHONPATH`.
3. Standalone/older-contract Talk suite with Agent overlay removed and provenance verified. On Windows use the Talk environment's Python with `-S`, manually add only Talk plus that environment's `site-packages`, remove inherited `PYTHONPATH`, and assert `importlib.util.find_spec("agent.realtime_voice_provider") is None` before invoking pytest so editable `.pth` files cannot silently load a sibling Agent checkout.
4. Ruff, compile, `git diff --check`, clean status, commit ancestry and changed-file audit.
5. Independent exact-commit specification/authority review.
6. Independent quality/security/concurrency review after spec approval.
7. Fix only concrete blockers under RED→GREEN, rerun affected reviews, then write a durable cross-repository handoff.

## Exit receipt

The slice is complete only when local immutable commits prove:

- canonical text remains host-owned;
- one explicit no-tool native response can be correlated to exact provider response/item IDs;
- exact response-local cancellation is replay-safe and race-safe;
- no generic/global cancellation is used;
- no PCM playback or physical-drain success is claimed;
- both repositories are clean and no deployment/publication occurred.
