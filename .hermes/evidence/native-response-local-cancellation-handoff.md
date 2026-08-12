# Native Response-Local Rendering and Cancellation Handoff

**Date:** 2026-08-11
**Status:** `slice-complete-final-reviews-approved`
**Scope:** provider-neutral explicit response and response-local cancellation contract in Hermes Agent, plus exact OpenAI Realtime rendering/cancellation adapter in Hermes Talk
**Deployment state:** local isolated implementation only

## Authority boundary

Canonical Hermes remains the sole authority for:

- durable session and operator identity;
- reasoning and canonical assistant text;
- tools and approvals;
- memory, background work, and persistence;
- exact durable assistant row and host turn marker;
- output ownership and future delivery completion.

Hermes Talk/OpenAI Realtime owns transport/rendering facts only:

- one explicit no-tool native audio response for already-canonical text;
- opaque provider correlation;
- provider response/item/audio/transcript lifecycle;
- exact response-local cancellation;
- passive input-speech-start timing.

No provider tool registry, second executor, client-authored history authority, generic TTS fallback, global cancellation fallback, PCM playback, physical drain, replacement-turn admission, installation, activation, or live canary was introduced.

## Isolated repositories

### Agent candidate

- Path: `C:\Users\Degen\isolated-dev\hermes-agent-native-cancel-20260811`
- Branch: `feat/native-response-local-cancel`
- Approved code head: `78b830ff163ccb359d9245eef323d897058b1143`
- Slice 1 code base: `08b62a2ff88bc501d58fc983379562d765494ecc`
- Worktree clean when sealed.

Stack above Slice 1:

1. `ff6c3a6a1` — static claim lookup rejects fabricated realtime claims
2. `ebd8e8dd3` — explicit native response contract
3. `cb299516c` — exact request/audio/no-tools hardening
4. `e8e73090f` — retain explicit sends across public waiter cancellation
5. `6e35ec1ee` — preserve intentional-close cancellation truth
6. `817b2a9ab` — eager-safe response-send bookkeeping
7. `b7dc26330` — frozen implementation plan (docs only)
8. `b499e6119` — provider-neutral response-local cancellation contract
9. `78b830ff1` — avoid retained-operation cleanup self-deadlock

### Talk candidate

- Path: `C:\Users\Degen\isolated-dev\hermes-talk-native-cancel-20260811`
- Branch: `feat/native-response-local-cancel`
- Approved code head: `389f046f8b266272fee8642d86e2d94ceb474192`
- Pinned Talk base: `99d8f2a3971db074bd2649c56d9afc91b63c57e2`
- Worktree clean when sealed.

Commits:

1. `b0ad78c0` — consume typed realtime setup fields
2. `65695fe8` — render canonical responses through Realtime
3. `2ba7e79f` — enforce canonical digest and lifecycle completion
4. `0b68001a` — reject pre-lifecycle provider output
5. `2c8be7be` — use typed automatic-response setup in core session caller
6. `389f046f` — exact response-local cancellation and passive speech-start

## Frozen plan

- Agent path: `.hermes\plans\2026-08-11-native-response-local-cancel.md`
- Plan commit: `b7dc26330562acdf9592bef8f413230670486aae`
- SHA-256: `12fafce374108c97fd5542a3832df2e7729a8dfc25242bf0b754c7e54d948898`
- Status: `frozen-after-one-adversarial-review`

The one planning review returned four concrete corrections. They were applied once; no second planning-review loop was run.

## Implemented Agent contract

`agent/realtime_voice_provider.py` now provides:

- passive frozen `InputSpeechStarted(item_id, audio_start_ms)`;
- exact primitive, trim, length, and nonnegative-offset validation;
- distinct `RealtimeCapability.RESPONSE_CANCELLATION`;
- public `cancel_response(response_id)` and protected `_cancel_response` hook;
- exact ID/capability/closed-terminal admission before provider I/O;
- duplicate-before-capacity ordering;
- bounded, non-evicting accepted cancellation tombstones;
- bounded in-flight cancellation operations;
- retained tasks that isolate provider sends from public waiter cancellation;
- late-success tombstoning and late-failure terminal truth;
- intentional-close markers pinned across failed close;
- eager-task publication and stale-callback identity guards;
- independently retained and exception-observed cleanup after send/cancel failure.

A final review found a real operation → close → operation deadlock. Deterministic REDs proved the cycle for both cancellation and inherited explicit response sends. Commit `78b830ff1` transferred cleanup ownership to an independently observed close task while preserving the original provider-hook exception and first terminal failure.

## Implemented Talk adapter

### Typed setup and optional surfaces

Talk detects baseline input, explicit-response, and response-cancellation surfaces independently. New typed fields are used for:

- `automatic_response=False`;
- exact input audio;
- exact output audio.

The current provider advertises a capability only after its complete production hook/event/cleanup path exists. Legacy and dependency-absent lanes remain additive.

### Exact explicit response

The core lane sends one pure request:

- `type: response.create`;
- `conversation: none`;
- one input-text message containing exact canonical text;
- render-verbatim instructions;
- audio-only output;
- validated session voice;
- PCM16 little-endian, 24 kHz, mono output;
- `tools: []`;
- `tool_choice: none`;
- opaque high-entropy correlation metadata only.

Host durable IDs, assistant row IDs, turn markers, and content digests are not exposed as provider metadata.

Talk reserves bounded pending/active/completed state before wire I/O, binds only real provider response/item IDs, and stores compact identities—not canonical text, audio, credentials, or raw provider envelopes.

### Exact output lifecycle

For a bound response, Talk enforces exact-once ordered progression:

1. `response.output_item.added`;
2. `response.content_part.added` with `output_audio`;
3. audio/transcript streaming;
4. `response.output_audio.done`;
5. terminal output transcript;
6. `response.content_part.done`;
7. `response.output_item.done`;
8. `conversation.item.done`;
9. strict `response.done`.

Recognized output before the first item stage fails closed before decode, state mutation, or emission.

Completion additionally requires:

- exact correlation and response/item continuity;
- one assistant `message` item;
- one `output_audio` part;
- optional `object` only as exact `realtime.item`;
- at least one nonempty audio delta;
- audio EOS;
- terminal transcript;
- exact SHA-256 equality between terminal transcript UTF-8 and the canonical host digest;
- no function/tool authority anywhere in the recognized envelope.

Malformed, out-of-order, replayed, late, wrong-ID, tool-bearing, digest-mismatched, or invalid-UTF-8 events terminate visibly and clear all response state.

### Exact response-local cancellation

Talk sends only:

```json
{
  "type": "response.cancel",
  "event_id": "<fresh unpredictable ID>",
  "response_id": "<exact active provider response>"
}
```

The legacy global `CancelResponse` encoding remains unchanged but is not used by this core lane.

Before wire I/O, Talk requires the exact response to be active and bound, reserves it in bounded cancellation state, and rejects unknown/completed/duplicate/concurrent cancellation.

A valid provider `status=cancelled` terminal requires:

- exact active response and correlation;
- a previously requested cancellation;
- absent/empty output when no item was bound; or one partial, already-bound assistant audio item;
- no terminal-minted item authority;
- optional exact `object: realtime.item` only;
- absent/null status details, or exact cancellation-compatible details with reason `client_cancelled` or `turn_detected`;
- no unknown/error/tool/function authority.

Only after side-effect-free validation does Talk clear exact state, record compact response/correlation tombstones, and emit one `Interruption` with the original host turn marker.

If strict completed output wins after a cancel send, completion remains authoritative and the cancellation reservation is cleared. If cancellation wins, later deltas/completion fail through tombstone/unbound-response checks.

`input_audio_buffer.speech_started` maps only to passive `InputSpeechStarted`; it does not cancel, fence playback, or admit a canonical replacement turn.

## TDD receipts

Durable RED evidence included:

- missing passive event/capability surface;
- direct cancellation await/capacity timeout;
- intentional-close classification failure;
- retained cancellation cleanup self-deadlock;
- analogous retained explicit-send cleanup self-deadlock;
- typed `automatic_response` provider-option shadowing;
- missing typed input/output setup declarations;
- missing independent optional-surface detection;
- missing explicit response builder and response ledgers;
- live `realtime.item` fixture not completing;
- canonical transcript mismatch incorrectly completing;
- 11 lifecycle omission/order failures;
- pre-start audio producing zero protocol failures;
- neighboring core-session typed setup incompatibility;
- missing exact cancellation builder;
- malformed speech-start values accepted;
- absent cancellation capability/hook.

Later tests that first ran green after shared machinery landed are not represented as chronological REDs.

## Verification receipts

### Agent

- Contract suite at approved tip: `130 passed`
- Combined Agent/gateway focused gate: `169 passed`
- Ruff: passed
- `py_compile`: passed
- `git diff --check`: passed
- Worktree: clean

### Talk with exact Agent dependency

- Task 2 compatibility gate: `43 passed`
- Explicit rendering initial gate: `148 passed`
- Corrected rendering gate: `164 passed`
- Typed core-session focused gate: `190 passed`
- Cancellation focused gate: `193 passed`
- Final full Talk suite: `976 passed, 1 skipped`
- Ruff: passed
- `py_compile`: passed
- `git diff --check`: passed
- Worktree: clean

Some reviewer commands used narrower or dependency-absent matrices and therefore reported different totals. The authoritative full exact-dependency controller and final-review command both reported `976 passed, 1 skipped` with imports resolving to Agent `78b830ff`.

## Review dispositions

### Agent contract

- Initial specification/authority review of `b499e6119`: `PASS`
- Initial quality/security/concurrency review: `REQUEST_CHANGES`
  - blocker: retained cancellation failure could deadlock with provider cleanup
- Corrected specification closure at `78b830ff1`: `PASS`
- Corrected quality/security/concurrency review at `78b830ff1`: `APPROVED`

### Talk explicit rendering

- Initial spec review at `65695fe8`: `FAIL`
  - canonical transcript digest not enforced
  - required item/content progression not enforced
- Closure at `2ba7e79f`: `FAIL`
  - recognized pre-start output could be emitted
- Final review at `0b68001a`: rendering state machine probes passed, but full-suite caller compatibility blocked
- Caller fix `2c8be7be`
- Final specification/authority review at `2c8be7be`: `PASS`
- Final quality/security/concurrency review at `2c8be7be`: `APPROVED`

### Talk response-local cancellation

- Exact-commit specification/authority review at `389f046f`: `PASS`
- Exact-commit quality/security/concurrency review at `389f046f`: `APPROVED`
- No critical or important issues remain.

## Explicit exclusions

Not implemented or claimed:

- Discord PCM sink or mixer ownership;
- 24 kHz mono to 48 kHz stereo framing/resampling;
- bounded playback backpressure/spool;
- output lease generation fencing;
- provider EOS plus physical drain completion receipt;
- exact playback interruption receipt;
- replacement transcript buffering/admission barrier;
- controller barge-in task;
- production construction/activation seam;
- plugin installation or gateway restart;
- live provider cancellation proof;
- Discord voice canary;
- push, PR, merge, deploy, or public release.

## Next authorized engineering boundary

The next slice is **response-local native PCM playback ownership**, not activation.

It must introduce an adapter-owned, generation-fenced playback lease bound to:

```text
canonical response identity
+ provider response ID
+ provider item ID
+ output generation
```

Minimum requirements:

1. exact PCM16LE 24 kHz mono ingress;
2. arbitrary-chunk carry handling;
3. bounded asynchronous backpressure;
4. conversion/framing to Discord 48 kHz stereo 20 ms frames;
5. one exact response-local playback owner;
6. provider EOS distinct from physical drain;
7. awaitable physical drain receipt;
8. exact lease interruption and stale-generation rejection;
9. no generic TTS/attachment fallback;
10. deterministic cancellation/completion/drain races.

Only after that playback lease is independently approved should a controller barge-in barrier combine:

- passive speech-start;
- exact provider response cancellation;
- exact playback lease interruption;
- provider terminal receipt;
- physical stop/drain receipt;
- bounded replacement transcript buffering;
- next canonical turn admission exactly once.

## Resume checklist

1. Verify Agent head `78b830ff163ccb359d9245eef323d897058b1143` and clean status.
2. Verify Talk head `389f046f8b266272fee8642d86e2d94ceb474192` and clean status.
3. Read this handoff and the frozen plan.
4. Load `native-playback-ownership-lifecycle.md`, `native-realtime-output-integration.md`, and `response-local-barge-in-barrier.md`.
5. Write one short source-anchored playback-lease plan with one adversarial review maximum.
6. Create fresh isolated candidates from these approved code heads.
7. Do not install, restart, activate, or run a live canary without explicit approval and a real production construction seam.
