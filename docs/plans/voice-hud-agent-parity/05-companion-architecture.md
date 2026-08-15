---
title: Hermes Native Realtime HUD — Companion Architecture
status: architecture-frozen-after-post-pr-final-review
date: 2026-08-11
product_owner: SmokeDev
program: Voice HUD Agent Parity
intent_authority: 01-intent-prd.md
repositories:
  - NousResearch/hermes-agent
  - TheSmokeDev/hermes-talk
---

# Hermes Native Realtime HUD — Companion Architecture

> **Document posture:** This document translates the frozen Intent PRD into a
> provider-neutral component, authority, lifecycle, data, failure, testing, and
> rollout architecture. It is not implementation authorization, a merge or
> publication decision, an installation or restart request, a production
> activation claim, or permission to run a live canary.

## 1. Architecture decision

The Native Realtime HUD is an attached transport for one already-running
canonical Hermes session. It is not a second model loop.

The architecture has four authority zones:

1. **Canonical Hermes** owns session identity, prompt/context, reasoning, model
   selection, tools, approvals, memory, persistence, delegation, Kanban, coding
   agents, background work, and the authoritative response.
2. **Gateway attachment host** binds an authenticated surface and operator to the
   exact canonical session, serializes turns, issues native-render requests, and
   reduces lifecycle evidence into truthful user-visible state.
3. **Realtime provider adapter** carries authorized audio, transcription,
   host-issued native rendering, normalized interruption timing, and provider
   transport lifecycle. It owns no Hermes tools or conversational authority.
4. **Surface adapter** owns microphone capture, authenticated speaker attribution,
   bounded PCM playback, exact playback leases, and HUD projection. It does not
   reason or reinterpret lifecycle state.

The first proof is Discord. Desktop and Quick Entry later consume the same host
contracts without creating a new authority model.

## 2. Governing invariants

These are architecture invariants, not implementation preferences:

1. One finalized authorized utterance creates at most one canonical user row and
   one canonical Hermes turn.
2. The provider receives no Hermes tool schemas and dispatches no Hermes tools.
3. Voice cannot add, remove, or override the attached session's model, prompt,
   tools, policy, approvals, skills, plugins, memory, or persistence path.
4. Only an exact persisted terminal assistant row may be claimed for native
   rendering.
5. The host issues the render request with `allow_tools=False` and automatic
   provider response disabled.
6. An attached native turn is fenced at admission before generic streaming-TTS
   setup; native delivery is then reserved at finalization before any remaining
   generic voice/TTS/audio-attachment path can claim the turn.
7. Provider output is untrusted until its identity and canonical-text fidelity
   are proven. Unvalidated PCM is never played.
8. One exact playback lease owns one response, turn marker, transport generation,
   and bounded byte stream.
9. User-visible `COMPLETED` requires canonical persistence and an exact normal
   playback-drain receipt. Provider completion alone is insufficient.
10. `INTERRUPTED` requires exact-response cancellation acknowledgment and, iff a
    playback lease opened, exact-lease interruption acknowledgment. It is
    mutually exclusive with `COMPLETED`.
11. Barge-in never cancels the canonical session or unrelated Hermes-owned work.
12. A finalized replacement utterance never enters an in-flight canonical turn.
    It is pinned and admitted exactly once after the prior barrier becomes
    terminal.
13. Surface close owns transport cleanup only. It cannot redefine ownership of
    Hermes tools, delegation, background work, or durable state.
14. Every queue, replay ledger, response spool, task, reconnect attempt, and lease
    is bounded and has one terminal cleanup owner.
15. Failure is visible and fail-closed. There is no silent fallback to generic
    TTS, Edge, ElevenLabs, `.ogg`, audio attachment, or a second agent.

## 3. Exact source snapshots and current evidence

The architecture is grounded in immutable local source snapshots. These are
implementation inputs, not deployment receipts.

| Repository/slice | Exact commit | What exists | Focused evidence run 2026-08-11 |
|---|---|---|---|
| Hermes canonical attachment/ingress | `d03d60fe3cafe9289513e53bacef0330e3916509` | host-minted invocation, exact session binding, canonical ingress | incorporated into later focused suites |
| Hermes native response/controller | `faf4f4559f3e8605046aa8a5ed54406e646a2b35` | explicit response contract, controller output state, playback lease protocols | `174 passed in 5.16s` |
| Hermes Discord generic-output sibling | `86c756de8c69a938db26ae5fd23829315cae8fa4` | changes attached input to `MessageType.VOICE`, which can activate generic auto-TTS; it is not a native-output dependency | `61 passed in 12.46s` |
| Talk current `main` | `28d7068521b3e3e4d2e37a17220a5698455d86aa` | API-v2 input-only OpenAI provider registration; legacy provider-owned join remains | source inspection only |
| Talk canonical attachment branch | `99d8f2a3971db074bd2649c56d9afc91b63c57e2` | `/talk core join`, capture-only Discord bridge, exact installed-host test | `45 passed, 1 skipped in 0.56s` |
| Hermes AG-UI PR #65845 | base `c0106e50e7ecedb3ce34e785d949725dc4e0e457`, head `b036d8be6d9786a7117777c8c3c2b40a84d2ca3b` | fresh-`AIAgent` HTTP/SSE adapter with event bridge and park/resume approvals; reference only, not a Talk dependency | open/blocked; no CI; local lock/export checks fail |

Current limitations:

- no one reviewed candidate stack contains all slices;
- the native controller is not production-wired to canonical delivery;
- Talk's core provider is input-only;
- no exact Discord production `NativeAudioOutputSink` exists;
- `faf4f455` and `86c756de` are sibling branches over the canonical-ingress
  base; the latter's generic `MessageType.VOICE` trigger must not be applied as
  native provenance or allowed to claim output before native reservation;
- current canonical attachment is idle-only and has no pinned replacement lane;
- current controller prototypes can expose canonical `COMPLETED` before playback
  drains;
- current output prototypes can write PCM before canonical-text fidelity is
  proven;
- no bounded real native canary has been authorized or run.
- PR #65845 reconstructs history from client messages and creates a fresh
  `AIAgent` for each run; that is intentionally not the durable-session ingress
  model for Talk. Its current head also lacks the AG-UI lockfile metadata needed
  for frozen installs.

## 4. System context

```text
Authenticated operator
        |
        v
+-------------------------- Active surface ---------------------------+
| Discord voice now; Desktop/Quick Entry later                       |
| speaker attribution | microphone capture | HUD | PCM playback lease |
+-----------------------------+---------------------------------------+
                              | authorized bounded PCM / receipts
                              v
+---------------------- Hermes Gateway attachment host ---------------+
| opaque attachment capability | exact session binding                |
| turn admission/serialization | delivery reservation                 |
| canonical-response claim     | lifecycle evidence reducer           |
+--------------------+-------------------------------+-----------------+
                     |                               |
       canonical MessageEvent                       | provider-neutral
                     v                               | realtime contract
+---------------- Canonical Hermes ----------------+ v
| same durable session, prompt, model loop, tools, | +----------------+
| approvals, memory, delegation, Kanban, persistence| | Realtime       |
| exact terminal assistant row and receipts         | | provider       |
+---------------------------------------------------+ | audio/STT/TTS  |
                                                      | no tools/agent |
                                                      +----------------+
```

No provider event bypasses the attachment host into canonical execution. No
canonical tool call flows through the provider.

## 5. Component model

### 5.1 Plugin command invocation capability

**Owner:** Hermes Gateway.

A contextual `/talk core join` invocation receives a host-minted,
nonserializable, consume-once capability only while the exact authenticated
command task is active. Successful return commits the provisional capability.
The capability binds:

- platform and surface identity;
- authenticated operator principal;
- profile and routing key;
- durable session ID;
- thread/guild scope;
- exact live adapter identity;
- routing/selection generation.

The plugin cannot mint, clone, serialize, persist, or broaden it. Open fails on
route drift, adapter replacement, session replacement, generation drift, or
second consumption.

The Discord speaker principal is an exact positive native platform ID. No
string/float/boolean coercion, display name, SSRC, provider label, transcript,
or voiceprint may be promoted into that principal.

Mute first revokes audio admission for the exact generation, then stops capture
and flushes only that generation's unsent input queue. Unmute requires the same
live binding and a fresh capture generation; stale callbacks remain fenced.
Detach is always available and cannot be blocked on provider or playback work.

Existing source anchor:
`gateway/realtime_voice_invocation.py:PluginCommandInvocation` and
`RealtimeVoiceAttachmentFactory`.

### 5.2 Gateway realtime attachment

**Owner:** Hermes Gateway.

The attachment is the narrow plugin-facing facade. It exposes only:

- lifecycle projection reads;
- authorized speaker PCM feed;
- synthesized all-zero silence feed;
- immediate host-enforced mute/unmute for the exact capture generation;
- immediate detach/close;
- surface interrupt request where allowed.

It does not expose the canonical runner, tool registry, session database,
approval records, provider credentials, or arbitrary output sinks.

Target change: the host, not Talk, obtains the exact output sink from the
captured Discord adapter. A plugin-supplied sink cannot become playback truth.

Existing source anchor:
`gateway/realtime_voice_messaging_host.py:GatewayRealtimeVoiceAttachment`.

### 5.2.1 Talk-first universal host seam

PR #65845 proves that external surfaces need typed run lifecycle, interrupt, and
terminal-event discipline. It does not prove that its HTTP/SSE protocol or
fresh-agent/history reconstruction should become the Hermes core contract.

The minimum universal seam is the existing host-owned attachment model, proven
first by Talk. It has only five surface-neutral capabilities:

1. open one consume-once attachment bound to an authenticated principal, route,
   and exact durable Hermes session;
2. admit one authorized user turn through the ordinary canonical serializer;
3. receive one opaque canonical-finalization receipt after durable persistence;
4. request a typed, explicitly scoped transport interruption without implying
   canonical/background-work cancellation;
5. observe typed lifecycle/terminal receipts and close transport resources.

Do not build a generic surface framework in the Discord MVP. Talk implements
these capabilities against the existing realtime attachment first. A later AG-UI
or Desktop adapter may consume the same host seam only after replacing
client-supplied history and fresh-`AIAgent` construction with exact canonical
session attachment.

Reference patterns worth carrying forward from PR #65845 are per-run event
bridge ownership, closing every open lifecycle before a terminal error,
fail-closed resume decisions, identity-scoped compare-and-swap cleanup, typed
behavioral config, and secrets-only environment variables.

Explicitly not reused for Talk: FastAPI/SSE, CopilotKit shared state, frontend or
state-writer tools, process-global tool registration, eager copied tool catalogs,
client-authored conversation history, the resume monkeypatch, a fresh agent per
request, or transport disconnect as authority to cancel Hermes-owned work.

### 5.3 Realtime voice controller

**Owner:** Hermes Gateway.

One controller owns one attachment generation and:

- provider session open/close/reconnect;
- bounded input-audio queue;
- normalized provider event pump;
- final-transcript admission coordination;
- one pinned replacement slot;
- one active native response identity;
- bounded pre-play output spool;
- exact playback lease lifecycle;
- exact-response cancellation;
- transport task/resource cleanup;
- monotonic internal evidence events.

It never invokes tools or selects a Hermes model.

Existing source anchor:
`gateway/realtime_voice_controller.py:GatewayRealtimeVoiceController`.

### 5.4 Canonical messaging host

**Owner:** Hermes Gateway.

The host consumes final transcripts and synthesizes normal `MessageEvent`
instances for the exact captured route. It verifies the ordinary canonical
handler durably produced:

- one matching user row;
- zero or more ordinary tool rows;
- one terminal assistant row;
- the exact durable session identity.

It returns an opaque, consume-once `CanonicalTurnReceipt`. It later resolves the
exact assistant row from canonical storage and builds the native response
request. Text supplied by provider metadata or plugin code is never accepted as
the canonical response.

Existing source anchors:

- `gateway/realtime_voice_messaging_host.py:GatewayRealtimeVoiceMessagingHost`;
- `_finalize_realtime_voice_event`;
- `claim_native_response`.

### 5.5 Canonical Hermes turn runner

**Owner:** Hermes Gateway and canonical agent runtime.

Voice follows the ordinary selected-session turn path, including:

- existing model/profile routing;
- already-resolved prompt and context;
- dynamic tools and plugins;
- approval policy and approval surfaces;
- tool persistence and receipts;
- memory, delegation, Kanban, cron, and coding-agent behavior;
- ordinary background-work ownership.

The voice attachment contributes only an opaque claim used to prove exact route
and finalization identity. It cannot mutate canonical runtime configuration.

### 5.6 Native delivery arbiter

**Owner:** Hermes Gateway/adapter delivery boundary.

This is a required new production seam with two linearization phases.

At **turn admission**, an attached native claim fences the exact turn before the
Gateway can set up streaming TTS. The fence suppresses streaming-TTS consumer
creation and whole-file/generic voice output for that turn key even if a sibling
path later marks the event `MessageType.VOICE` or `/voice on` is active.

After **canonical finalization** and before remaining voice/TTS/audio delivery:

1. inspect the host-minted attachment claim;
2. atomically reserve native delivery for the exact
   `(durable_session_id, assistant_message_id, turn_marker, generation)`;
3. consume the canonical finalization receipt;
4. suppress every generic voice/TTS/audio-attachment path for that turn;
5. issue the native response request;
6. preserve ordinary Discord text delivery exactly as it behaves for the current
   MVP; do not add a transcript-mirroring switch in this slice.

If reservation fails, native failure is visible. It does not silently become a
generic voice response. Ordinary non-attached typed turns remain unchanged.

The seam must linearize before any adapter path starts voice rendering. A
post-send hook is too late.

### 5.7 OpenAI Realtime provider adapter

**Owner:** `hermes-talk` provider edge.

Talk reuses its auth resolver and low-level realtime wire but expands the current
input-only API-v2 adapter with:

- `EXPLICIT_RESPONSE`;
- `RESPONSE_CANCELLATION`;
- normalized response start/completion;
- bounded output transcript events;
- bounded PCM output events;
- normalized input speech-start timing;
- terminal provider failures.

Provider setup is exact:

- `automatic_response=False`;
- tools empty;
- tool choice disabled where the provider protocol supports it;
- no copied Hermes instructions, history, memory, approvals, or tool registry;
- exact configured model/voice;
- exact input/output PCM formats;
- unknown output/tool events fail closed.

The host-issued response is out-of-band from provider-owned conversation. Its
only authoritative content is the canonical text in `RealtimeResponseRequest`.
Provider correlation metadata echoes response identity but confers no authority.

Existing source anchor:
`talk_core_realtime.py:TalkOpenAIRealtimeProvider` and
`_TalkCoreRealtimeSession`.

### 5.8 Canonical-text fidelity gate

**Owner:** Gateway controller, with provider evidence.

`gpt-realtime` is generative. A request to “say this exactly” is not sufficient
proof that the generated PCM represents the exact canonical result. The MVP
therefore uses a bounded pre-play gate:

1. reserve native output;
2. send digest-bound canonical text with tools and automatic response disabled;
3. retain provider PCM in a bounded response spool;
4. retain normalized output transcript in a bounded transcript ledger;
5. require one terminal transcript exactly equal to the canonical UTF-8 text;
6. require matching response/turn/generation identity and provider completion;
7. only then open the adapter playback lease and release PCM to playback.

Mismatch, missing final transcript, duplicate terminal transcript, metadata drift,
spool overflow, timeout, or provider error destroys the spool and emits
`NATIVE_FAILED` before playback. There is no generic fallback.

If the selected provider cannot produce byte-exact terminal transcript evidence
for canonical text, it is not qualified for this frozen MVP. The architecture
must not weaken “exact canonical response” into best-effort paraphrase.

Before the Discord playback-sink and barge-in slices, a bounded, separately
authorized provider-qualification probe sends representative non-sensitive
canonical UTF-8 samples, including punctuation, Markdown, newlines, and Unicode.
The product owner records the acceptable byte-exact pass criterion before the
probe. Failure blocks the provider/MVP rather than weakening the fidelity gate.

This correctness-first gate may increase time-to-first-audio. The first canary
records latency; product latency budgets remain intentionally deferred.

### 5.9 Discord native PCM sink

**Owner:** captured Hermes Discord adapter.

The exact live Discord adapter mints one `DiscordNativeAudioPlaybackLease` for
the exact active voice connection. Talk does not provide a truth receipt for
host playback.

The sink:

- validates captured guild/channel/voice-client identity;
- requires exact `pcm_s16le`, 24 kHz, mono input;
- uses bounded PCM buffering;
- tracks local player consumption by lease and byte count;
- prevents concurrent leases for the same attachment;
- drains normally or interrupts exactly once;
- fences old generations;
- returns an opaque receipt validated by the minting sink;
- closes idempotently and releases the player source.

Normal receipt fields:

- lease ID;
- provider response ID;
- turn marker;
- transport generation;
- exact bytes written and consumed;
- `interrupted=False`.

Interrupted receipt carries the same identity with `interrupted=True` and proves
that queued PCM for that lease was discarded and the player no longer owns it.

Local player/adapter drain is not proof a human heard audio. Human audibility is
recorded separately in the authorized bounded canary.

The implementation may extract tested queue/source techniques from Talk's
`DiscordAudio`, but Hermes core must not import the Talk plugin. The production
sink belongs at the Discord adapter edge.

### 5.10 HUD evidence reducer

**Owner:** Hermes host.

The HUD does not infer state from strings, timers, provider prose, or queue
length. It projects host-reduced typed evidence.

Internal states and user-visible states are distinct. In particular,
`CANONICAL_FINALIZED` is not user-visible `COMPLETED`.

The reducer accepts only:

- host attachment lifecycle;
- canonical turn lease and tool/approval lifecycle;
- canonical finalization receipt;
- provider response identity and fidelity result;
- exact playback lease start/drain/interruption receipt;
- cleanup and reconnect receipts.

## 6. Data contracts

### 6.1 `RealtimeSessionBinding`

Immutable identity:

```text
profile_id
routing_key
runtime_session_id
durable_session_id
provider_session_id
selection_generation
surface_id
operator_principal
```

### 6.2 `CanonicalTurnReceipt`

Consume-once host proof:

```text
durable_session_id
turn_marker
user_message_id
assistant_message_id
```

The host validates receipt object identity, not field equality.

### 6.3 `RealtimeResponseRequest`

Host-issued render request:

```text
durable_session_id
assistant_message_id
turn_marker
canonical_text
sha256(canonical_text UTF-8)
exact output audio format
allow_tools = false
```

The provider never chooses or replaces this text.

### 6.4 `PinnedUtterance`

One bounded replacement record:

```text
binding identity
provider item/turn identity
transport generation
exact final transcript text
received sequence/time
admission state = PINNED | SUBMITTED | REJECTED
```

The first valid final wins. Duplicate identity is ignored; conflicting or second
final fails visibly. Close destroys an unsubmitted pin unless the host has
already taken canonical ownership.

The pin reserves the next canonical user-turn position for that session once the
prior terminal barrier releases. Typed input arriving after the pin is retained
by the ordinary adapter serializer behind it; typed input that already owned the
canonical turn before the pin remains ahead of it. This preserves the frozen
"next serialized turn" promise without injecting into or reordering an already
owned turn.

### 6.5 `PlaybackLease`

Opaque sink-minted ownership:

```text
lease_id
response_id
turn_marker
transport_generation
format
bytes_written
bytes_consumed
terminal_outcome
```

A forged field-equivalent receipt is invalid.

### 6.6 `DeliveryOutcome`

Exactly one terminal outcome:

```text
COMPLETED_NORMAL_DRAIN
INTERRUPTED_ACKNOWLEDGED
FAILED_CLOSED
```

No path may emit two outcomes.

## 7. End-to-end turn flow

### 7.1 Attach

1. Smoke invokes `/talk core join` in the exact Discord session.
2. Discord authenticates the platform principal.
3. Gateway mints contextual invocation authority.
4. Talk captures a provisional attachment factory.
5. Successful command return commits it.
6. Talk opens the exact registered provider and capture-only surface.
7. Host resolves the exact live Discord adapter and mints the output sink.
8. Controller opens the provider with no tools and no automatic response.
9. HUD projects `CONNECTING`, then `LISTENING` only from provider readiness and
   active capture evidence.

### 7.2 Input and canonical turn

1. Discord surface admits PCM only from the bound operator principal; only exact
   all-zero synthesized silence may omit speaker attribution.
2. Provider emits partial transcripts for projection only.
3. Provider emits one final transcript with bounded item/turn identity.
4. Host checks binding, generation, replay ledger, route state, and speaker
   authority.
5. If no turn/delivery is active, host consumes one permit and synthesizes the
   normal `MessageEvent`.
6. Gateway claims the ordinary turn lease without yielding.
7. Canonical Hermes executes unchanged reasoning, tools, approvals, and
   persistence.
8. Gateway reads back one matching user row and one terminal assistant row and
   mints `CanonicalTurnReceipt`.

### 7.3 Native output

1. The admission-time native fence is already held for this exact turn; the
   delivery arbiter now reserves final native ownership before remaining generic
   voice delivery.
2. Host consumes the finalization receipt and rereads the exact assistant row.
3. Host builds digest-bound `RealtimeResponseRequest`.
4. Provider renders out-of-band with tools/automatic response disabled.
5. Controller spools bounded PCM and output transcript.
6. Fidelity gate validates exact canonical text and identity.
7. Captured Discord adapter opens the exact playback lease.
8. Controller writes validated PCM to that lease.
9. Provider completion transitions to drain waiting, not completion.
10. Exact normal drain receipt closes the lease.
11. Evidence reducer emits user-visible `COMPLETED` exactly once.

### 7.4 Barge-in

1. Authorized input audio produces a real provider speech-start event.
2. Provider event supplies timing only; host confirms one active response and
   records whether an exact playback lease has opened.
3. Controller increments interrupt generation and fences new delivery events.
4. Controller sends exact provider response cancellation.
5. If a playback lease opened, controller calls its exact `interrupt_and_wait`;
   otherwise it destroys the bounded pre-play spool without minting a lease.
6. Old response reaches `INTERRUPTED` after provider cancellation acknowledgment
   and, iff a lease opened, exact lease interruption acknowledgment.
7. Provider continues authorized capture/transcription for the correction.
8. The first valid replacement final is pinned.
9. After the old delivery barrier is terminal and the canonical assistant row is
   already durable, host submits the pin once as the next canonical turn.
10. Old response can never later emit `COMPLETED`.

### 7.5 Speech finalized while Hermes is thinking

1. If a valid final arrives while a canonical turn is active but no playback
   lease exists, it is pinned rather than rejected or injected.
2. Canonical turn continues unchanged.
3. After its terminal assistant row is durable and its delivery barrier reaches a
   terminal outcome, the pin is admitted exactly once.
4. Additional pending finals exceed the MVP one-slot contract and fail visibly.

## 8. State machines

### 8.1 Attachment lifecycle

```text
DETACHED
  -> CONNECTING
  -> LISTENING
  -> RECONNECTING -> CONNECTING | FAILED
  -> CLOSING
  -> CLOSED
```

`RECONNECTING` is transitional. It is never terminal.

### 8.2 Canonical turn lifecycle

```text
FINAL_TRANSCRIPT
  -> ADMISSION_RESERVED
  -> ADMITTED
  -> THINKING
  -> USING_TOOL <-> AWAITING_APPROVAL
  -> CANONICAL_FINALIZED
  -> DELIVERY_RESERVED
```

Tool and approval states come only from Hermes lifecycle evidence.

### 8.3 Native delivery lifecycle

```text
DELIVERY_RESERVED
  -> PROVIDER_RENDERING
  -> FIDELITY_VALIDATED
  -> PLAYBACK_LEASED
  -> SPEAKING
  -> DRAINING
  -> COMPLETED
```

Terminal alternatives:

```text
PROVIDER_RENDERING/FIDELITY_VALIDATED/PLAYBACK_LEASED/SPEAKING/DRAINING
  -> INTERRUPTING -> INTERRUPTED
  -> FAILED
```

`CANONICAL_FINALIZED` is intentionally not `COMPLETED`.

Pre-lease interruption from `PROVIDER_RENDERING` or `FIDELITY_VALIDATED` requires
provider cancellation acknowledgment and spool destruction but no impossible
lease receipt. Once `PLAYBACK_LEASED` is reached, exact lease interruption
acknowledgment is additionally mandatory.

### 8.4 Replacement lifecycle

```text
EMPTY -> PINNED -> SUBMITTED -> EMPTY
                 -> REJECTED -> EMPTY
```

Only the exact attachment owner may consume the pin.

## 9. Concurrency and linearization points

Required linearization points:

1. contextual capability mint and successful-handler commit;
2. factory consume before provider open awaits;
3. route-generation validation and permit reservation;
4. admission-time native fence before streaming-TTS setup;
5. canonical turn-slot claim before any turn await;
6. one finalization receipt after canonical persistence read-back;
7. native delivery reservation before remaining generic voice routing;
8. finalization receipt consumption and canonical-row reread;
9. response dispatch identity publication before provider events can be handled;
10. fidelity terminality before playback lease open;
11. lease terminal outcome before user-visible terminal delivery state;
12. interrupt generation increment before cancellation awaits;
13. pinned utterance consume after prior delivery terminality;
14. close ownership retained independently of any cancelling waiter.

All old-generation events are ignored or fail closed. No cleanup step releases an
identity or resource owned by a newer generation.

## 10. Security and approval boundary

### 10.1 Identity

Identity comes from the authenticated platform adapter and host attachment
binding. It never comes from:

- transcript text;
- voiceprint;
- provider speaker label;
- provider item ID;
- correlation metadata;
- channel presence alone.

### 10.2 Tools

The provider receives no tool schema. Unknown function-call or assistant-owned
provider conversation events are protocol failures. Hermes alone selects and
runs tools.

### 10.3 Approvals

MVP approvals remain visual/text Hermes approvals. Provider audio cannot mint,
resolve, replay, or reinterpret an approval. `AWAITING_APPROVAL` is projected
only from an actual Hermes approval record.

### 10.4 Shared-surface disclosure

Discord playback is disclosure to every authorized listener in that voice
surface. Sensitive output fails closed or requires a private surface/explicit
operator decision under existing policy. Provider rendering does not weaken
that policy.

For the Discord MVP, the default policy is `operator_only`: native rendering is
eligible only when the voice member set is exactly the bound operator and the
connected Hermes bot. Membership is resolved by the
captured Discord adapter at reservation and revalidated before playback. A
membership lookup failure, join/leave race, or any extra principal—human or
bot—fails
closed before PCM playback. Broader shared playback requires a separately
recorded, exact-surface operator decision and is not part of the minimum canary.

### 10.5 Configuration authority

Non-secret behavior belongs in typed `config.yaml`/existing setup UX, not new
`HERMES_*` environment variables. The MVP configuration surface must include:

- native realtime enabled/disabled, default disabled;
- provider registration name;
- exact model and voice;
- exact input/output audio formats;
- queue, transcript, spool, and timeout ceilings;
- Discord disclosure policy, default `operator_only`;
- redacted receipt retention duration;
- activation generation/manifest identity.

Secrets remain in the existing provider credential mechanism and never appear
in configuration receipts.

## 11. Privacy and retention

Provider-bound data is limited to:

- current authorized microphone audio;
- bounded transcript request state;
- exact canonical render text for one response;
- non-secret response/turn correlation identity;
- configured model, voice, and transport options.

Provider-bound data excludes:

- Hermes credentials or provider tokens from other systems;
- tool schemas or tool outputs beyond the final canonical text;
- approval records;
- full conversation history;
- system prompt or memory corpus;
- unrelated session context;
- browser/computer state;
- delegation internals.

Runtime retention:

- microphone PCM: memory only in the bounded 32-chunk input queue; released as
  each chunk is sent and fully discarded on interrupt/reconnect/close; never
  written to disk by the MVP;
- partial transcript: memory only, maximum 32,768 characters, replaced by newer
  evidence and discarded on final/terminal/close;
- final transcript: persisted once only through canonical user history;
- canonical render text: existing canonical assistant history is authoritative;
  transport copies live only through the active response and are released on
  terminality/close;
- output PCM spool: memory only for the MVP, hard-capped at 32 MiB and 120
  seconds of response lifetime, destroyed on drain/interruption/failure/close;
- provider output transcript: memory only unless redacted receipt policy explicitly
  permits a digest; raw text is released with the response;
- receipts: IDs, digests, timings, byte counts, outcomes, and error classes only,
  retained in the operator-owned local audit bundle for seven days by default
  and then deleted; a bounded canary bundle may be retained longer only by an
  explicit operator decision;
- logs: no audio, canonical text, credentials, secret values, or raw provider
  payloads by default.

The 32 MiB/120-second spool and seven-day receipt defaults are typed config
ceilings. Lower values may be selected before activation; increases require a
new review because they expand disclosure/resource risk.

Provider-side retention remains subject to the configured provider account and
policy and must be documented before activation.

## 12. Failure semantics

| Failure | Required behavior |
|---|---|
| unauthorized speaker | discard before provider/canonical admission; negative receipt |
| stale/wrong generation | fail closed; no canonical row |
| duplicate final transcript | replay-ignore exact duplicate; reject conflict |
| busy route with first replacement | pin once; never inject |
| pinned-slot overflow | visible failure; preserve first pin |
| provider tool/output before host request | protocol failure and close |
| provider send failure | native failed; destroy spool; no fallback |
| transcript fidelity mismatch | destroy spool before playback; native failed |
| PCM spool overflow/timeout | close response; native failed |
| playback lease open/write failure | cancel provider response; close lease; native failed |
| provider completes without bytes/transcript | native failed |
| drain timeout | interrupt/close exact lease; native failed, never completed |
| interruption partial failure | fail closed and close attachment; never completed |
| reconnect during native output | fail current delivery and close; no ambiguous replay |
| surface close | stop capture/provider/spool/lease/reconnect; preserve Hermes-owned work |
| host shutdown | bounded transport cleanup; no claim process-local work survived restart |

## 13. Resource ownership

| Resource | Owner | Terminal cleanup |
|---|---|---|
| Discord receive tap | active surface | detach generation and restore exact prior listener |
| input audio queue | controller | drain/discard and join audio pump |
| provider websocket/session | provider session/controller | terminal close and joined event pump |
| replay ledger | controller/admission | clear on attachment close |
| pinned utterance | host/controller | submit once or destroy on close/failure |
| response spool | controller | delete on playback terminality/failure/close |
| playback queue/source | exact lease | normal drain, interrupt, or failed close |
| reconnect task/token | controller | join/cancel before close receipt |
| canonical turn/tool/delegation | Hermes | existing Hermes lifecycle only |

A zero-resource close receipt names every transport-owned resource class and
proves no live task, queue, spool, tap, provider session, reconnect attempt, or
lease remains.

## 14. Observability and receipts

Receipts are structured and content-minimal:

- attachment ID and generation;
- profile/routing/durable-session digests or redacted IDs;
- operator/platform authorization outcome;
- provider session/response/turn IDs;
- canonical user/assistant row IDs;
- canonical content digest, not raw text;
- playback lease ID, bytes written/consumed, outcome;
- sequence numbers and monotonic timestamps;
- task/resource counts at close;
- error class and bounded code, not secret-bearing exception text.

Latency observations:

- final transcript -> canonical admission;
- canonical final -> provider request accepted;
- canonical final -> first adapter audio;
- provider speech-start -> interruption barrier acknowledgment;
- reconnect start -> listening;
- close request -> zero-resource receipt.

No pass/fail latency budget is invented before the product owner approves one
from real evidence.

## 15. Test architecture

### 15.1 Provider contract

- tools and automatic response rejected;
- exact setup/output formats;
- explicit response identity and replay;
- output event normalization;
- response cancellation races;
- unknown tool/output events fail closed;
- send/close/reconnect failure cleanup.

### 15.2 Canonical admission

- exact session/profile/operator binding;
- same typed and voice prompt/tool/model/policy path;
- behavior-level capability-invariance evidence using the same resolved runtime
  identity and redacted prompt/tool/policy snapshot digests; no sensitive prompt
  text is emitted and source-regex/change-detector tests do not substitute;
- one user and one terminal assistant persistence identity;
- unauthorized/stale/replayed/duplicated/wrong-generation negatives;
- slash-shaped transcript cannot gain command authority;
- one-slot replacement serialization.

### 15.3 Native output

- atomic reservation before generic voice routing;
- consume-once finalization and canonical-row reread;
- text digest and output format;
- pre-play fidelity match/mismatch;
- bounded spool overflow/timeout;
- no generic TTS/audio attachment fallback;
- one lease, one response, one generation.

### 15.4 Playback and barge-in

- bounded queue and byte accounting;
- normal drain receipt;
- interruption receipt;
- drain/interrupt mutual exclusion;
- late provider events after interruption;
- replacement final before/during/after cancellation;
- close during every await boundary;
- listener/player generation replacement.

### 15.5 Work ownership and approvals

- harmless read-only dynamic tool uses ordinary authentication/policy;
- deterministic approval projection tests where a live harmless tool needs none;
- delegated non-mutating work continues after barge-in/surface close;
- surface close cannot invoke canonical cancellation;
- no host-restart durability overclaim.

### 15.6 Cross-repository candidate

One exact Agent commit and one exact Talk commit must run together in a fresh
environment. Unit mocks do not replace the installed-plugin integration test.
Independent review targets exact final diffs and race evidence.

### 15.7 Minimum canary operations

The harmless dynamic-tool step is fixed to the ordinary Hermes
`skills_list(category="autonomous-ai-agents")` tool and verifies that the
`hermes-agent` skill is present. This is read-only, deterministic for the exact
reviewed candidate environment, and exercises dynamic tool discovery/execution
through canonical Hermes. If that exact skill/category is unavailable in the
candidate manifest, Gate E is blocked rather than silently substituting a tool.

The separate non-mutating delegation step uses ordinary `delegate_task` with a
bounded child instructed to perform no tool calls and return exactly
`native-hud-delegate-ready`. The receipt must identify the work as process-local,
prove it remains observable after surface close/barge-in, and make no host-restart
survival claim.

Neither operation naturally requires live approval. Deterministic tests must
therefore prove that real Hermes approval records alone drive
`AWAITING_APPROVAL`, and that provider speech cannot create or resolve one.

## 16. Delivery slices

Implementation remains blocked until architecture review/freeze. When authorized,
use these reviewable slices:

1. **Candidate integration spine** — build one fresh Agent worktree from the
   canonical session runtime and port only reviewed ingress/output seams. Fence
   each native-attached turn at admission before streaming or whole-file generic
   TTS can start.
   Do not cherry-pick `86c756de` as a native-output dependency; reconcile or
   remove its generic `MessageType.VOICE` trigger only after atomic native
   reservation exists.
2. **Lifecycle vocabulary correction** — replace early user-visible
   `COMPLETED` with internal `CANONICAL_FINALIZED` and evidence-reduced terminal
   delivery.
3. **Canonical delivery reservation** — linearize native ownership before generic
   voice/TTS/audio paths while preserving ordinary typed behavior.
4. **Talk explicit output adapter** — tools-off host-issued response,
   cancellation, normalized output events, no provider-owned dialogue.
5. **Provider qualification gate** — separately authorized, non-sensitive,
   byte-exact representative-text probe; block later native-output slices if the
   provider fails rather than weakening the gate.
6. **Fidelity spool** — bounded transcript/PCM gate with fail-closed mismatch.
7. **Discord playback sink** — adapter-minted exact leases, drain/interruption
   receipts, bounded PCM.
8. **Pinned replacement and barge-in** — exact response/optional-lease barrier
   and one-slot serialized correction.
9. **Truthful HUD projection** — typed host evidence only.
10. **Cross-repository security/concurrency review** — exact final diffs and tests.
11. **External activation package** — exact SHAs, source mapping, health checks,
    rollback, and bounded canary script; execution still requires separate
    operator approval.

One slice must not claim a later gate.

## 17. Rollout gates mapped to architecture

### Gate A — Canonical ingress and parity

Requires exact attachment binding, canonical turn receipt, provider zero-tool
proof, same-session typed/voice invariance, replay negatives, and focused tests.

### Gate B — Native canonical output

Requires admission-time generic-TTS fencing, atomic final reservation,
canonical-row claim, explicit provider response, completed provider
qualification, fidelity validation, exact Discord playback lease, normal drain
receipt, and negative proof that generic voice paths were not called.

### Gate C — Barge-in and lifecycle

Requires exact cancellation plus lease interruption, mutually exclusive terminal
outcome, one pinned replacement admission, background-work preservation, and
zero-resource close under races.

### Gate D — Surface integration

Requires truthful Discord state and visible recovery/failure. It authorizes only
the bounded Discord proof after separate approval.

### Gate E — Activation and live proof

Requires exact candidate SHAs, clean independent reviews, fresh-process source
mapping, rollback proof, non-sensitive canary plan, operator approval, and
post-canary restoration. No document alone authorizes this gate.

## 18. Rollback architecture

Before activation, create an external operator-owned manifest containing:

- prior reviewed Agent/Talk SHAs and source mappings;
- candidate SHAs and package hashes;
- process owner and exact startup command;
- enable/disable flags;
- health commands;
- rollback commands;
- expected ordinary typed-session receipt;
- expected zero-realtime-resource receipt.

Rollback:

1. detach new voice routing;
2. terminate candidate process through its owner;
3. restore exact prior reviewed source mapping;
4. start a fresh process;
5. prove ordinary typed behavior in the same durable history;
6. prove native provider/capture/spool/playback resources are absent;
7. record the receipt.

In-process source swapping is not rollback proof.

## 19. Deferred decisions and extension points

The architecture deliberately leaves these product decisions open:

- spoken progress;
- text transcript mirroring during native playback;
- latency budgets and percentile gates;
- background speech after HUD focus loss;
- spoken approval ceremony;
- wake phrase;
- additional realtime providers.

Extension points exist only at provider and surface boundaries. None may change
the canonical authority model.

## 20. Rejected alternatives

### Realtime provider as the agent

Rejected because it creates a second prompt, context, tool policy, memory, and
response authority.

### Copied fixed Hermes tool registry

Rejected because it drifts from dynamic tools/plugins/skills and bypasses normal
approval/session ownership.

### AG-UI adapter as Talk's canonical session transport

Rejected for the Talk MVP. PR #65845 builds a fresh `AIAgent` per request,
accepts client-supplied conversation history, owns process-local run/approval
registries, and uses an adapter-side resume monkeypatch. Those are reasonable
AG-UI compatibility techniques but do not prove attachment to the already-running
durable Hermes session. Talk may reference its event-terminality and race tests;
it must enter Hermes through the five-capability host attachment seam instead.

### Generic STT -> Hermes -> generic TTS as the target

Rejected as the product target because it cannot prove native realtime
response/cancellation semantics. It may remain ordinary Hermes behavior outside a
native-owned turn.

### Immediate playback of unvalidated provider PCM

Rejected because a generative provider may paraphrase canonical text. Playback
must wait for bounded fidelity evidence in the MVP.

### Talk plugin as playback receipt authority

Rejected because the host must derive completion from the exact captured platform
adapter/voice connection, not a plugin-supplied claim.

### Provider completion equals delivery completion

Rejected because provider generation says nothing about adapter/device drain.

### Surface close cancels all related work

Rejected because canonical tools and delegated/background work belong to Hermes,
not the transport.

## 21. Architecture review questions

Independent review must challenge at least:

1. whether pre-play transcript fidelity is sufficient and safely bounded;
2. whether OpenAI Realtime can qualify for byte-exact canonical output without a
   stronger API guarantee;
3. native reservation linearization against every generic delivery path;
4. exact Discord drain semantics and lease forgery resistance;
5. speech-start/final/cancel/close race ordering;
6. one-slot pinned replacement overflow behavior;
7. controller close ownership under waiter cancellation;
8. route and generation drift during long provider sessions;
9. provider payload minimization and retention;
10. rollback and zero-resource evidence.

## 22. Architecture definition of done

This architecture is ready to freeze only when:

- independent architecture/security/concurrency review is complete;
- every finding is dispositioned and corrected in this document;
- the intent/architecture reconciliation has no contradiction;
- exact document hashes are recorded;
- the status is explicitly changed from draft to frozen;
- implementation remains separately authorized.

The product implementation is done only under the stricter Definition of Done in
the frozen Intent PRD.
