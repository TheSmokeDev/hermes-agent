# Native PCM Playback Ownership Implementation Plan

> **For Hermes:** Implement task-by-task under strict RED→GREEN TDD. One adversarial plan review only; move deeper risk discovery into executable race tests and final code review.

**Goal:** Render the exact persisted canonical assistant row through the already-open Realtime provider into Discord using one Agent-owned, response-local PCM playback lease, and report completion only after provider EOS plus a generation-qualified local Discord sender-drain receipt.

**Architecture:** The canonical messaging host reserves native output from its consume-once `RealtimeVoiceFinalizationReceipt`, reloads and validates the persisted assistant row, and gives the controller only compact canonical identity plus the live text needed for one retained provider send. The production trigger is the existing `AdmissionStatus.SUBMITTED` branch: it must pass `AdmissionResult.receipt` into a new consume-once host reservation method and launch the retained native send; messaging playback does not depend on `project_host()`, which currently has no production messaging caller. `GatewayRealtimeVoiceController` binds the provider response/item/generation, acquires one opaque lease from the captured Discord adapter, streams bounded PCM with backpressure, and treats `ResponseCompleted` as provider EOS—not audible completion. The Discord adapter owns conversion, framing, coexistence, sender-boundary acknowledgement, and lease invalidation; Talk remains capture-only and owns no Discord playback.

**Tech Stack:** Python 3.11–3.14, asyncio, stdlib `struct`/`deque`/threading synchronization, discord.py's existing sender thread and host-owned `VoiceMixer`, pytest/pytest-asyncio. No new package, ffmpeg subprocess, temporary audio file, NumPy requirement, generic TTS fallback, activation, or canary.

**Pinned inputs:**
- Agent base: `78b830ff163ccb359d9245eef323d897058b1143`
- Talk provider dependency: `6c3ed3a24cb87b2b493ba5b7faca8d116f9d5bd1`
- Agent implementation clone: `C:\Users\Degen\isolated-dev\hermes-agent-native-playback-20260812`
- Branch: `feat/native-playback-ownership-20260812`

**Honest completion boundary:** Discord provides no remote-speaker/device acknowledgement. “Drained” means the exact final lease frame was returned by the mixer and the discord.py sender thread advanced to its next read, proving the prior synchronous `send_audio_packet()` call returned. It does not claim remote receipt or that a human heard it.

---

## Non-negotiable invariants

1. Agent remains the only reasoning/tool/approval/persistence authority. Provider tools remain empty/inert and automatic responses remain off.
2. One canonical assistant row produces at most one native rendition. Reserve before generic TTS can claim the turn; native failure never silently falls back to generic TTS.
3. Immutable binding chain: exact host finalization receipt → persisted assistant row/digest → transport generation → provider correlation/response/item → adapter lease ID.
4. Talk remains the provider adapter and capture-only transport. It never acquires, writes, drains, cancels, or releases Discord playback.
5. All output state and queues are bounded. PCM may backpressure or fail; it may never be silently dropped and later reported complete.
6. Provider EOS and local sender drain are separate receipts. Strong completion requires both.
7. Late callbacks from generation N cannot write, finish, abort, or release generation N+1.
8. This slice does not implement speech-start barge-in or replacement-turn admission. It does implement the response-local lease interruption primitive and close/reconnect fencing needed by that future barrier.

---

### Task 1: Add a dependency-safe host-owned streaming lease to `VoiceMixer`

**Objective:** Create one exact lease that accepts 24 kHz mono PCM incrementally, converts and frames it losslessly, coexists with existing mixer children, and returns identity-protected finish/interruption receipts.

**Files:**
- Modify: `plugins/platforms/discord/voice_mixer.py`
- Test: `tests/gateway/test_discord_voice_mixer.py`

**Step 1 — RED: immutable identity and strict construction**

Add tests for exact built-in bounded `lease_id`, `response_id`, `turn_marker`, positive exact generation, one active holder, and custom `__eq__` lookalikes. Assert forged field-equivalent receipts fail adapter-owned identity validation.

Run:
```bash
python -m pytest -q tests/gateway/test_discord_voice_mixer.py -k native_lease_identity
```
Expected: valid failure because no lease API exists.

**Step 2 — GREEN: minimal contract types**

Add frozen receipt/identity types and a mixer-owned streaming child. Keep receipt construction private to the mixer/lease. Expose async operations:
- `write_pcm(data)`
- `finish_and_wait(timeout)`
- `interrupt_and_wait(timeout)`
- `close()`

Use exact holder identity and idempotent close. One mixer may own at most one native lease in this slice.

**Step 3 — RED/GREEN: deterministic conversion and arbitrary chunks**

Tests must freeze:
- `[0, 2, -3]` → exact stereo vector `[0,0,0,0, 1,1,2,2, 0,0,-3,-3]`;
- signed boundaries and truncation toward zero;
- one-shot vs every split vs one-byte writes;
- at most one pending provider byte;
- 480 mono samples / 960 bytes → one 3,840-byte Discord frame;
- odd total bytes fail at EOS; a later byte may complete the sample;
- final partial frame pads only at EOS while accepted-byte count excludes padding.

Implement with explicit little-endian `struct` operations, one-byte input carry, previous-sample interpolation state, and exact 3,840-byte frames. Do not use `array('h')` without explicit endian handling.

**Step 4 — RED/GREEN: bounded backpressure**

Use `asyncio.Event`/thread barriers, never sleeps. Fill a small configured frame capacity; prove `write_pcm` blocks, sender consumption releases it, ordering is exact, accepted bytes advance only after irrevocable sink acceptance, caller cancellation cannot create an uncounted successful write, and no drop occurs.

Use a lock-protected deque plus loop-thread futures/notifications; never hold a threading lock across `await`.

**Step 5 — RED/GREEN: sender-drain receipt**

At the start of each `VoiceMixer.read()`, acknowledge the prior lease frame: discord.py could only call the next read after the prior synchronous `send_audio_packet()` returned. Synthetic underrun silence must not advance lease counters. At EOS, require one subsequent read to acknowledge the final real/padded frame before resolving `finish_and_wait`.

Assert receipt identity, provider byte count, lease frame count, `interrupted=False`, exact-once completion, and timeout/failure without a receipt.

**Step 6 — RED/GREEN: response-local interruption and coexistence**

Two ordinary speech/ambient children plus a native lease: interrupting the lease clears only its queued/carry state, never ambient or ordinary speech. Test cancel-before-read, cancel-after-read-before-next-read, repeated interrupt/close, write-after-terminal, and old-generation callbacks. Interruption receipt must be holder-qualified and must not claim normal drain.

**Step 7 — Remove runtime NumPy dependency from the mixer core**

Refactor frame summing/clamping to stdlib explicit int16 operations so native playback works under the Discord messaging install without NumPy. Preserve ambient/speech mixing bytes with golden tests. `synth_ambient_pcm` may remain optional, but `VoiceMixer.read()` and native streaming must not import NumPy.

Run:
```bash
python -m pytest -q tests/gateway/test_discord_voice_mixer.py
python -m ruff check plugins/platforms/discord/voice_mixer.py tests/gateway/test_discord_voice_mixer.py
python -m py_compile plugins/platforms/discord/voice_mixer.py tests/gateway/test_discord_voice_mixer.py
```

Commit:
```bash
git add plugins/platforms/discord/voice_mixer.py tests/gateway/test_discord_voice_mixer.py
git commit -m "feat(discord): add bounded native PCM playback lease"
```

---

### Task 2: Expose the lease through the captured Discord adapter

**Objective:** Make the existing host-owned Discord connection the only lease minting/revocation authority without letting Talk touch `VoiceClient.play()` or `stop()`.

**Files:**
- Modify: `plugins/platforms/discord/adapter.py`
- Modify: `tests/gateway/test_discord_voice_mixer.py`
- Test: `tests/gateway/test_voice_command.py`

**Step 1 — RED: exact adapter acquisition authority**

Test exact guild, current `VoiceClient` object identity, connected state, current mixer generation, exact input format (`pcm_s16le`, little-endian, 24 kHz, mono), and exclusive holder. Wrong/stale/fake values fail before mixer mutation.

**Step 2 — GREEN: `acquire_native_playback_lease(...)`**

Add one narrow adapter method. The adapter—not Talk or controller—selects the current voice client and mixer and mints the lease. Capture a per-guild connection generation/voice-client identity. Return an opaque lease only after exact validation.

If no continuous mixer exists, install a **silent native playback bus** under the guild voice lock before acquisition. This is adapter ownership, not plugin takeover. The native installer must not call `_get_ambient_pcm()` or synthesize/install ambient unless the master `voice_fx.enabled` setting is already true. If the voice client is already playing a non-mixer legacy source, fail acquisition with a bounded busy result (or wait only under an explicit bounded arbitration primitive); never call `vc.stop()` merely to obtain a native lease. Never pop `_voice_mixers`, park `play_in_voice_channel`, or expose private `VoiceClient` ownership to Talk.

**Step 3 — RED/GREEN: silent installation, busy arbitration, and invalidation**

Prove disabled `voice_fx` installs a silent bus with no ambient/ack side effect and no generic playback. Prove an already-playing legacy source is not stopped or replaced and acquisition fails boundedly. Then prove leave, client replacement, mixer replacement, or mixer `after` failure invalidates active leases, wakes blocked writers/drainers, and prevents stale release from touching a replacement. Adapter cleanup must retain enough exact identity to close once.

**Step 4 — RED/GREEN: coexistence and arbitration**

Prove ordinary `play_in_voice_channel`, acknowledgements, ambient audio, and native lease remain callable and isolated. Define native lease as one speech child; ordinary host speech may coexist but cannot be mistaken for native drain. Acquiring/writing/finishing an active-bus lease must make zero additional `VoiceClient.play()`/`stop()` calls.

Run:
```bash
python -m pytest -q tests/gateway/test_discord_voice_mixer.py tests/gateway/test_voice_command.py
python -m ruff check plugins/platforms/discord/adapter.py tests/gateway/test_voice_command.py
python -m py_compile plugins/platforms/discord/adapter.py tests/gateway/test_voice_command.py
```

Commit:
```bash
git add plugins/platforms/discord/adapter.py tests/gateway/test_discord_voice_mixer.py tests/gateway/test_voice_command.py
git commit -m "feat(discord): expose generation-fenced playback leases"
```

---

### Task 3: Mint canonical native-output reservations from durable host truth

**Objective:** Convert a consume-once finalization receipt into one exact provider request and one adapter playback authority without trusting caller text or IDs.

**Files:**
- Modify: `gateway/realtime_voice_messaging_host.py`
- Test: `tests/gateway/test_realtime_voice_messaging_host.py`

**Step 1 — RED: reserve before row lookup/provider I/O**

Add a consume-once bounded reservation ledger keyed by exact receipt object identity. Concurrent projection attempts yield one winner. Forged/field-equivalent receipt, stale factory/routing generation, replaced session entry/adapter, and repeated claims fail closed.

**Step 2 — RED/GREEN: reload canonical assistant row**

From the exact durable session and assistant row ID in the host-minted receipt, require one terminal assistant string row, no tool calls, exact captured entry and adapter, and a stable SHA-256 digest. Caller-supplied `HostProjection.detail` is never speech text.

Return a compact reservation containing durable session ID, assistant message ID, turn marker, digest, exact output format, attachment/routing generation, and adapter playback-acquisition capability. Keep canonical text only for the immediate provider request operation.

**Step 3 — RED/GREEN: expose the consume-once reservation to the controller**

Add a provider-neutral host protocol method such as `reserve_native_output(binding, receipt)`. It must require the exact `RealtimeVoiceFinalizationReceipt` minted by this host, consume it once, reread canonical row truth, and return the compact reservation plus immediate live text needed for one send. Tests must prove the exact receipt returned by `FinalTranscriptAdmission.admit()` is the only production bridge; a caller-created or replayed receipt fails.

**Step 4 — RED/GREEN: reserve generic-delivery ownership**

Integrate reservation at the existing canonical native-event delivery boundary before generic whole-file or streaming TTS can claim it. Preserve canonical text delivery/persistence while proving generic TTS, `play_tts`, and attachment voice fallback remain untouched on both success and native failure.

Run:
```bash
python -m pytest -q tests/gateway/test_realtime_voice_messaging_host.py
```

Commit:
```bash
git add gateway/realtime_voice_messaging_host.py tests/gateway/test_realtime_voice_messaging_host.py
git commit -m "feat(gateway): reserve canonical native voice output"
```

---

### Task 4: Bind provider output to the lease in the controller

**Objective:** Start one explicit response and consume normalized output events into the exact lease without blocking the provider event pump during drain.

**Files:**
- Modify: `gateway/realtime_voice_controller.py`
- Test: `tests/gateway/test_realtime_voice_controller.py`

**Step 1 — RED/GREEN: production receipt trigger and retained explicit response send**

In `_handle_event(InputTranscript)`, when `FinalTranscriptAdmission.admit()` returns `AdmissionStatus.SUBMITTED`, require its exact `result.receipt`, pass it immediately to the host's consume-once native reservation method, and launch the retained response-send task. This is the production messaging trigger. Do not wait for or depend on `project_host()`. Atomically publish compact request state, mark `send_dispatched` immediately before `await session.start_response(request)` with no intervening await, and retain the send independently of caller cancellation. Request fields must come only from the host reservation and set `allow_tools=False`.

If a native reservation is active, `project_host(COMPLETED)` may record canonical persistence but must not emit the controller's strong `COMPLETED` lifecycle. Strong completion is emitted only by the validated native drain path. A failed native rendition remains failed and does not downgrade to ordinary completed delivery.

Test provider `ResponseStarted` arriving after hook wire placement but before hook return; pre-dispatch starts fail. Test late success/failure, duplicate projection, eager task completion, and close races.

**Step 2 — RED/GREEN: exact response/item/generation binding**

Accept `ResponseStarted` only for the reserved turn and current transport generation. Acquire one lease after response binding and before first PCM. Bind the first item once. Wrong turn/response/item, continuation, duplicate start, unsolicited response, stale generation, or property/equality lookalikes fail before mutation.

**Step 3 — RED/GREEN: ordered PCM writes with backpressure**

Route aligned nonempty `OutputAudio` through `await lease.write_pcm`. Count bytes only after success. No controller-side unbounded queue. Blocked writes, external close, spontaneous cancellation, sink failure, and stale callbacks must converge once without deadlock or false byte counts.

**Step 4 — RED/GREEN: provider EOS then retained drain**

`ResponseCompleted` requires prior audio and exact bound response/item/turn. Start a retained drain task and immediately return to the event pump. Strong completion appears only after exact receipt type/identity/generation/byte count validation and `interrupted=False`.

Test forged receipt, wrong count, validator exception, drain timeout/failure, caller cancellation, duplicate/stale callbacks, and exact-once lease close.

**Step 5 — RED/GREEN: interruption/close/reconnect fencing**

`Interruption`, session failure, controller close, and generation replacement fence writes and call only the exact lease's `interrupt_and_wait`/`close`. Any native ownership makes provider `SessionFailure` terminal; do not resume the same provider session while a retained send/lease/drain exists. Implement first-failure-owner exclusion so close cannot cancel the task preserving the original error.

Do not implement speech-start admission or replacement transcript buffering in this task.

Run:
```bash
python -m pytest -q tests/gateway/test_realtime_voice_controller.py
python -m ruff check gateway/realtime_voice_controller.py tests/gateway/test_realtime_voice_controller.py
python -m py_compile gateway/realtime_voice_controller.py tests/gateway/test_realtime_voice_controller.py
```

Commit:
```bash
git add gateway/realtime_voice_controller.py tests/gateway/test_realtime_voice_controller.py
git commit -m "feat(gateway): drain native realtime PCM through exact lease"
```

---

### Task 5: Prove the real canonical path end-to-end without activation

**Objective:** Exercise factory → canonical turn/persistence → explicit provider request → normalized Talk-shaped output → adapter lease → local sender drain.

**Files:**
- Modify: `tests/gateway/test_realtime_voice_messaging_integration.py`
- Modify only if required: `tests/tui_gateway/test_realtime_voice_host_integration.py`
- Talk remains read-only at `6c3ed3a...`

**Step 1 — RED/GREEN: installed fake-provider path**

Extend the existing real messaging-host integration. Assert:
- one `GatewayRunner`, Agent, SessionDB, SessionStore, host, controller, canonical user row, and canonical assistant row;
- one exact `RealtimeResponseRequest` using persisted assistant text/digest and `allow_tools=False`;
- Talk-shaped `ResponseStarted` → ordered PCM → transcript → `ResponseCompleted`;
- no controller completion before controlled sender-drain barrier;
- one validated playback receipt and one native rendition;
- zero generic streaming/whole-file TTS, `play_tts`, voice attachment fallback, provider tools, or second executor.

**Step 2 — RED/GREEN: negative matrix**

Wrong row/digest/turn/response/item/generation, output before reservation/start, post-EOS PCM, missing audio, lease failure, sender failure, and close during blocked write/drain must fail closed with no generic fallback and zero retained tasks/leases.

**Step 3 — cross-repository provider contract gate**

With `PYTHONPATH` pointing to this Agent candidate, run Talk's focused contract/core suites at `6c3ed3a...` and require a clean Talk worktree. No Talk code change unless a real normalized-contract incompatibility is demonstrated under RED.

Run Agent focused gates:
```bash
python -m pytest -q \
  tests/gateway/test_discord_voice_mixer.py \
  tests/gateway/test_voice_command.py \
  tests/gateway/test_realtime_voice_controller.py \
  tests/gateway/test_realtime_voice_messaging_host.py \
  tests/gateway/test_realtime_voice_messaging_integration.py
```

Run Talk dependency gate from the pinned Talk checkout:
```bash
PYTHONPATH='C:/Users/Degen/isolated-dev/hermes-agent-native-playback-20260812' \
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider \
  tests/test_core_realtime_adapter.py tests/test_openai_realtime.py \
  tests/test_realtime_contract.py tests/test_core_session.py
```

Commit:
```bash
git add tests/gateway/test_realtime_voice_messaging_integration.py tests/tui_gateway/test_realtime_voice_host_integration.py
git commit -m "test(gateway): prove canonical native PCM delivery"
```

---

### Task 6: Closure gates and handoff

**Objective:** Seal an immutable pre-barge-in candidate and decide whether to proceed to barge-in/activation planning.

1. Run the affected Agent suites, then the complete Agent suite supported by this checkout.
2. Run Ruff on every changed file, `compileall`/`py_compile`, `git diff --check`, secret/debug scan, and clean-tree check.
3. Run Talk focused and complete suites against the exact Agent candidate; rerun genuine Agent-absent Talk compatibility.
4. Run two independent read-only final reviews on the immutable SHA:
   - authority/spec/identity/provider-vs-drain truth;
   - concurrency/security/privacy/task/lease cleanup.
5. Preserve exact SHAs, commands, counts, RED→GREEN receipts, local sender-boundary definition, and all explicit exclusions in `.hermes/evidence/native-pcm-playback-handoff.md`.
6. Issue **GO/NO-GO only for the next barge-in barrier or activation-planning slice**. Do not install, restart, activate, join voice, or run a live canary in this plan.

Final exclusions remain explicit:
- no claim of remote Discord delivery or human audibility from local sender drain;
- no speech-start barge-in or replacement-turn admission yet;
- no generic TTS fallback;
- no push, PR, merge, install, restart, activation, or live canary.
