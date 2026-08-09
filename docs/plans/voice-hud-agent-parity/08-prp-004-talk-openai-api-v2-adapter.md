# PRP-004 — Hermes Talk OpenAI Realtime API-v2 Input Adapter

**Status:** Implementation-ready after one adversarial planning review  
**Hermes Agent base:** `30b93e2410c212b0c62e761e365b10e5aeb1f374`  
**Hermes Talk base:** `409787bf796e790050f35601fc9b1d0144e990e1`  
**Depends on:** PRP-002 provider-neutral admission; PRP-003 gateway realtime controller and TUI host  
**Repositories:** Hermes Agent (bounded registration receipt fix + contract); Hermes Talk (provider implementation)

## Outcome

Hermes Talk registers one real Hermes core `RealtimeVoiceProvider` API-v2 implementation named `talk_openai_realtime`. In PRP-004 the provider is deliberately **input-only**: it transports microphone PCM to OpenAI Realtime and emits exact, final/partial operator input transcripts for PRP-002/003 admission.

The core lane does not ask the OpenAI model to respond, does not emit assistant output, does not accept provider tool work, and does not execute Talk tools. It is a transcription transport over a long-lived realtime connection, not a second agent.

Registration does not activate a session. PRP-005 and PRP-006 own trusted host attachment, audio surfaces, and production activation. Talk's existing terminal/Discord relay remains an explicitly labeled `legacy-provider-executor` compatibility lane until later convergence slices retire it.

## Architectural facts established by review

1. Core API-v2 has no host-owned ordinary `response.create` hook. With provider automatic response disabled, API-v2 cannot initiate assistant output or provider tool calls.
2. OpenAI response callbacks do not identify the originating input item/turn. Response-to-turn causality cannot be inferred from callbacks or “latest input.” It may only be reserved when a future core-owned response request is sent.
3. Existing Talk minting defaults to `create_response=True` before a later WebSocket update. The core lane must disable automatic response in **both** configuration phases.
4. `PluginContext.register_realtime_voice_provider()` currently returns `None` for both acceptance and silent rejection. Truthful Talk receipts require a bounded Agent API fix returning the registry acceptance boolean.
5. A Talk plugin must still import and operate standalone when Hermes core is missing or API-incompatible.

## Non-goals

- No assistant output audio/transcript in the core lane.
- No explicit interruption capability; there is no core-lane provider response to interrupt.
- No provider response metadata echo.
- No tool calling, tool result submission, tool cancellation, or continuation.
- No output truncation, dynamic context, or session resumption.
- No Desktop HUD, terminal migration, or Discord migration.
- No canonical TTS/assistant-output bridge; that requires a future host-owned initial-response/output seam.
- No direct call to Talk's relay, Talk tool executor, Hermes tool dispatch, `AIAgent.run_conversation`, detached `hermes -z`, transcript persistence, or session mutation.
- No mutation of PRP-002 admission or PRP-003 controller/host semantics.

## Exact initial capability baseline

Provider and every opened session advertise exactly:

```python
frozenset({
    RealtimeCapability.INPUT_TRANSCRIPTION,
    RealtimeCapability.INPUT_COMMIT_EVENTS,
})
```

`INPUT_COMMIT_EVENTS` means `_commit_audio()` sends one OpenAI input-buffer commit command. The adapter may also observe OpenAI's server-VAD commit callback for mapping, but no new core event type is invented.

The adapter rejects:

- non-empty `RealtimeVoiceSetup.tools`;
- any provider option requesting automatic response;
- provider options that shadow core fields;
- audio formats other than the exact supported OpenAI PCM format;
- any setup asking for a capability outside the fixed provider declaration.

Unexpected `response.*`, output-audio, output-transcript, or function-call events are terminal protocol failures. They are not normalized as ordinary core output because no core-owned response request exists.

## Authority and compatibility contract

1. Production registration uses only `PluginContext.register_realtime_voice_provider(...)`.
2. Talk does not mutate `agent.realtime_voice_registry` directly.
3. Missing/incompatible core API or absent registration method produces `unsupported-optional`.
4. A present host method that raises produces a redacted `failed` receipt without affecting CLI, slash, hooks, TTS, or STT.
5. A host method returning `True` produces `registered`.
6. A host method returning `False` produces `rejected`; Talk must not report the provider as registered or available through core.
7. Provider name is `talk_openai_realtime`; API version is inherited from exact core API-v2.
8. `is_available()` is passive: no config write, install, HTTP request, socket, task, microphone, or secret logging.
9. Credentials are resolved only in `open_session()` and never stored in diagnostics/provider data.
10. Provider data is frozen diagnostic transport data only. It grants no operator, profile, routing, session, authorization, mutation, completion, or memory authority.
11. The single-speaker terminal/core adapter maps input speech as `OPERATOR` / `OPERATOR_INPUT`. PRP-006 must supply immutable Discord participant policy instead of reusing this mapping for arbitrary speakers.
12. Legacy Talk remains separate and status-labeled `legacy-provider-executor`, never `core-backed`.

## Bounded Hermes Agent change

Change `PluginContext.register_realtime_voice_provider(provider) -> bool`:

- return `False` for wrong type;
- return the exact `register_provider(provider)` boolean for API mismatch, built-in collision, or accepted registration;
- log accepted registration only when `True`;
- preserve existing plugin isolation and registry precedence.

Tests must cover:

- accepted API-v2 provider returns `True` and is present;
- wrong type returns `False` and is absent;
- API mismatch returns `False` and is absent;
- built-in collision returns `False` and leaves the built-in object unchanged;
- plugin loading remains enabled on rejection.

This boolean is registration acceptance, not provider runtime availability.

## Defensive Talk core boundary

Add `talk_core_realtime.py`. At import it probes all required symbols in one guarded block:

- `REALTIME_VOICE_PROVIDER_API_VERSION`;
- `RealtimeVoiceProvider`;
- `RealtimeVoiceSession`;
- `RealtimeVoiceSetup`;
- `RealtimeCapability`;
- `RealtimeAudioFormat`;
- `SessionReady`, `SessionClosed`, `SessionFailure`;
- `InputTranscript`;
- `TranscriptRole`, `TranscriptProvenance`.

The adapter is available only when the version is literal `2` and all symbols have the expected class/enum shape. Missing or incompatible core sets a private unavailable sentinel/factory; it must not leave a partially subclassed adapter or poison imports of Talk CLI/Discord/legacy modules.

`core_provider_available()` reports only contract import compatibility. `TalkOpenAIRealtimeProvider.is_available()` additionally performs passive Talk dependency/config readiness checks.

Standalone Talk must not import Hermes core transitively through `__init__.py` when the guarded adapter is unavailable.

## One shared OpenAI wire implementation

Do not copy the WebSocket client.

Refactor `talk_openai_realtime.py` to expose a private low-level wire session that owns:

- ephemeral session mint;
- HTTP/WebSocket allocation;
- serialized JSON send;
- raw validated JSON receive;
- abnormal EOF/error-frame classification;
- cancellation-safe/idempotent close.

Preserve the existing `OpenAIRealtimeSession` legacy Talk-neutral facade over that wire object. Add the core facade in `talk_core_realtime.py` over the same wire object. Existing legacy tests remain green except explicit status wording.

The low-level mint helper receives an explicit `automatic_response` argument. Legacy callers retain their current default. Core always passes `False`.

## Automatic-response prohibition

The core lane must prove both configurations contain `create_response=False`:

1. the HTTP ephemeral/client-secret mint payload;
2. the subsequent WebSocket `session.update` payload.

No transient true default is allowed before the WebSocket update. Any core provider option requesting `True` fails before mint/network allocation.

The core setup sends no tools and rejects non-empty core tools before mint/network allocation.

## Input identity mapping

The core session owns a bounded, non-evicting active/terminal input ledger.

OpenAI wire authority:

- provider session ID: `session.created.session.id`;
- input item ID: exact `item_id` from commit/transcription callbacks;
- provider turn ID: the exact same input item ID, explicitly aliased because OpenAI supplies no separate input turn identifier;
- transcript text/finality: exact input-transcription event fields.

Rules:

1. `SessionReady` emits once from exact `session.created`; repeated identical session ID is inert, changed ID is terminal.
2. Reserve an input item on commit or first transcript callback before any async handoff.
3. A partial transcript maps to literal `InputTranscript(final=False)`.
4. A completed transcript maps once to literal `InputTranscript(final=True)` with exact item/turn alias, role `OPERATOR`, provenance `OPERATOR_INPUT`.
5. Duplicate identical partials may be emitted for captions but cannot create final admission. Duplicate finals are terminally remembered and map to the same replay key; they never receive a new ID.
6. Conflicting text/finality for a terminal item, missing/blank/padded/oversized item ID, non-boolean finality, blank/oversized final text, or transcript before exact item association becomes one terminal `SessionFailure`.
7. Do not invent IDs from timestamps, counters, random values, transcript text, display names, response IDs, or provider metadata.
8. Active/terminal input ledger capacity is a positive finite construction value with a safe default of `1024`.
9. Capacity exhaustion emits one terminal `SessionFailure` and closes; it does not evict active or terminal identities.
10. Ledger clears only after proven terminal close.

No response, output-item, call, or batch ledger is implemented in PRP-004 because the core lane cannot create a causally bound response.

## Future response/tool causality (deferred contract)

A later API revision/slice may add a host-owned ordinary response request. Only then may Talk advertise output/tool/continuation capabilities.

Required future rules are recorded now to prevent inference shortcuts:

- response→turn association is reserved when the core-owned response request is sent;
- provider callbacks may confirm, never invent, that association;
- continuation uses a previously stored core batch→turn relation;
- provider function-call `output_index` is ordering authority;
- duplicate/conflicting output indexes fail closed;
- provider response ID is transport response identity, not automatically the core-owned batch ID;
- active inputs, responses, output items, calls, and batches each require explicit finite capacities;
- capacity exhaustion is terminal and does not evict reachable identity.

These future semantics are not capabilities or production code in PRP-004.

## Audio, commit, lifecycle, cleanup

- `send_audio()` sends immutable bytes through one core session and one wire send lock.
- MIME/sample-rate/channel mismatch fails before send.
- `_commit_audio()` sends one `input_audio_buffer.commit`.
- OpenAI server-VAD may commit automatically; explicit duplicate commit semantics must be deterministic and tested.
- WebSocket error frame, abnormal EOF, malformed JSON/protocol, send failure, setup failure, unsolicited response/output/tool event, ledger exhaustion, and stream exhaustion converge on one visible terminal failure/close path.
- Clean provider close emits one `SessionClosed`.
- Open cancellation closes partially allocated HTTP/WebSocket state.
- Close is core-base idempotent and cancellation-safe; repeated close and cancelled close leave no client/socket/task.
- No reconnect/resume capability is claimed.

## Talk registration and truthful diagnostics

Hermes Talk changes:

- `__init__.py`: independently attempt realtime provider registration only when `core_provider_available()`;
- `talk_tools.REGISTRATION_REQUIREMENTS`: add optional `realtime_voice_provider`;
- registration helper records `registered`, `rejected`, `failed`, or `unsupported-optional` from the boolean/exception/method-presence result;
- doctor/status distinguishes core contract availability, registration acceptance, passive provider availability, and legacy lane;
- plugin/docs no longer imply the core lane executes provider tool calls.

Receipt vocabulary is exactly:

- `registered`;
- `rejected`;
- `failed`;
- `unsupported-optional`.

Do not introduce `unsupported-old-host` or infer registry acceptance from a non-raising `None` return.

## Files

### Hermes Agent

Production:

- `hermes_cli/plugins.py` — boolean realtime registration receipt.

Tests/docs:

- `tests/hermes_cli/test_plugins_realtime_voice_registration.py`;
- `docs/plans/voice-hud-agent-parity/08-prp-004-talk-openai-api-v2-adapter.md`;
- `docs/plans/voice-hud-agent-parity/03-epic-index.md` after implementation evidence.

### Hermes Talk

Expected production:

- `talk_core_realtime.py`;
- `talk_openai_realtime.py` shared wire extraction/configuration;
- `talk_wire.py` explicit automatic-response mint parameter;
- `__init__.py` registration;
- `talk_tools.py`, `talk_doctor.py` truthful receipts/status;
- `pyproject.toml` ship new module without mandatory Hermes dependency;
- `README.md`, `docs/OPERATING.md`, `CHANGELOG.md`, `plugin.yaml` truthful lane labeling/version notes.

Expected tests:

- `tests/test_core_realtime_adapter.py`;
- `tests/test_register.py`;
- `tests/test_openai_realtime.py`;
- `tests/test_realtime_contract.py`;
- doctor/status and repository-hygiene tests.

## RED → GREEN

1. **Agent RED:** realtime registration acceptance and rejection are indistinguishable (`None`).
2. **Agent GREEN:** return exact boolean with accepted/wrong-type/API-mismatch/collision tests.
3. **Talk RED:** real API-v2 host loads plugin but receives no realtime provider.
4. **Talk GREEN:** defensive input-only provider registers; core-absent Talk remains healthy.
5. **RED auto-response:** inspect mint and update and observe legacy true default.
6. **GREEN auto-response:** core sends false in both phases and rejects true/non-empty tools before network.
7. **RED identity matrix:** missing/conflicting/duplicate/interleaved input IDs, malformed finality/text, capacity exhaustion.
8. **GREEN mapper:** exact bounded item→turn alias and final-only core events.
9. **RED unsolicited output:** response/output/function-call events attempt to enter core lane.
10. **GREEN fail-closed:** one terminal failure; no Talk relay/tool/agent call.
11. **RED teardown:** cancellation during mint/connect/send/close, error frame, abnormal EOF, repeated close.
12. **GREEN cleanup:** one terminal path and zero retained socket/client/task.
13. **Regression:** full Talk and focused Agent provider/plugin/controller suites remain green.

## Exact cross-repository conformance

Use the real worktrees:

```text
AGENT=C:\Users\Degen\isolated-dev\hermes-agent-talk-shared-controller-20260809
TALK=C:\Users\Degen\isolated-dev\hermes-talk-shared-controller-20260809
PY=C:\Users\Degen\isolated-dev\hermes-agent-realtime-transcript-admission-20260808\.venv\Scripts\python.exe
```

### Core-present process

Run with `PYTHONPATH=$AGENT;$TALK` and a temporary `HERMES_HOME`:

- load Talk through the real `PluginManager`/`PluginContext`;
- assert exactly one accepted `talk_openai_realtime` provider in the real registry;
- trap HTTP/WebSocket constructors, `asyncio.create_task`, controller/session constructors, microphone APIs, transcript DB writes, and agent construction during registration;
- prove registration creates none of them;
- open only with a fake shared wire and prove mint/update false, exact final transcript mapping, duplicate/capacity failure, unsolicited response failure, and cleanup.

### Core-absent process

Spawn a fresh Python process with a meta-path/import guard that raises `ModuleNotFoundError` for `agent` and `hermes_cli` while leaving Talk's declared third-party dependencies available. Load Talk by the same file-path package mechanism used by plugin tests and prove:

- CLI/legacy modules import;
- realtime receipt is `unsupported-optional` when registered against a stub old host;
- no partially defined core provider is exposed;
- legacy Talk tests/doctor remain usable.

Commands:

```bash
$PY -m pytest "$TALK/tests/test_core_realtime_adapter.py" "$TALK/tests/test_register.py" -q
$PY -m pytest "$AGENT/tests/hermes_cli/test_plugins_realtime_voice_registration.py" -q
python -m pytest -q   # from Talk worktree, full standalone suite
```

The conformance script/test logs the resolved Agent/Talk commit SHAs and temporary `HERMES_HOME`; it never reads or modifies the installed checkout.

## Gates

- strict RED → GREEN evidence;
- Talk full suite green from the clean PRP-004 worktree;
- Agent provider/registry/plugin/controller suites green against `30b93e241`;
- cross-repo core-present and core-absent conformance green;
- Ruff/format/type/compile/diff checks green;
- forbidden-boundary scan finds no core adapter call to Talk relay/tool execution, Hermes dispatch, `AIAgent.run_conversation`, detached `hermes -z`, transcript persistence, or host mutation;
- the one adversarial planning review corrections are incorporated; no second planning review;
- final independent spec review followed by quality/security/concurrency review;
- separate clean local commits in both isolated worktrees;
- normal push only; no force-push or merge without explicit direction.

## Backout

- Revert/disable optional core provider registration; retain legacy Talk.
- No conversation/provider-session data migration exists.
- Reverting PRP-004 does not alter canonical Hermes sessions, Quick Entry, Discord messaging, legacy Talk transcripts, or installed configuration.
- Do not restart or mutate live Discord gateway PID `49272` during adapter validation.
