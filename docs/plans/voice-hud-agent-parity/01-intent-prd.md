---
title: Hermes Native Realtime HUD — Intent PRD
status: intent-frozen-after-final-review
date: 2026-08-11
product_owner: SmokeDev
program: Voice HUD Agent Parity
repositories:
  - NousResearch/hermes-agent
  - TheSmokeDev/hermes-talk
---

# Hermes Native Realtime HUD — Intent PRD

> **Document posture:** This is the product-intent authority for the program. It
> defines the experience, ownership boundaries, success criteria, and rollout
> gates. It is not an implementation receipt, merge authorization, production
> activation claim, or permission to run a live canary.

## 1. Executive decision

Hermes will become an always-available, native realtime agent that the operator
can speak with while continuing to use the rest of the computer.

The realtime voice layer will not become a second agent. The already-running
canonical Hermes session remains the sole owner of reasoning, model selection,
context, memory, skills, tools, approvals, delegation, Kanban, persistence, and
durable history. The realtime provider supplies duplex audio, turn detection,
transcription, native speech rendering, and interruption signals only.

The product is successful when, for the exact attached canonical session with
unchanged configuration, speaking and typing use the same already-resolved
prompt/context, model loop, tools, policy, approvals, and persistence path.
Voice neither adds nor removes capability and never mutates the toolset or
system prompt mid-conversation. Native audio adds speed and continuity without
creating new authority.

## 2. Product intent

Hermes should stop feeling like a window the operator must switch into. It
should feel like a persistent HUD over the operator's work: summon it, speak
naturally, watch truthful state, let Hermes use its normal capabilities, hear
progress or results, interrupt when needed, and continue the same conversation
through text or voice without creating another agent or losing context.

The defining sentence is:

> **Voice is a live input/output transport for the canonical Hermes session—not
> a separate realtime agent with a copied prompt, copied memory, or smaller tool
> registry.**

## 3. Problem

Hermes already owns the capabilities the operator wants to use by voice:

- the canonical conversation and durable history;
- model/provider routing and prompt caching;
- dynamic tools, plugins, and skills;
- memory and vault access;
- approvals and operator identity;
- computer and browser control;
- email, calendar, files, documents, and external services;
- Kanban, cron, delegation, subagents, and coding-agent workflows;
- receipts, failures, retries, and persisted results.

A standalone realtime conversation can sound natural, but it does not
inherently inherit that authority. Giving the realtime model a fixed subset of
copied tools creates a parallel agent with different memory, permissions,
state, and failure behavior. It may feel like Hermes while being unable to do
what the real Hermes can do.

A simple speech-to-text and generic text-to-speech loop has the opposite
problem: it preserves the real Hermes brain but does not deliver native duplex
conversation, low-latency speech, precise interruption, or a trustworthy
realtime experience.

The product must combine both without duplicating either system:

1. canonical Hermes remains the brain and authority;
2. a realtime provider becomes native ears and mouth;
3. the active surface owns microphone and playback transport;
4. every user-visible state and completion claim traces to real lifecycle
   evidence.

## 4. Primary user

### Smoke — operator and authority owner

Smoke wants to operate Hermes by voice while working in Discord, Desktop, a
browser, an IDE, or another application. He expects the same Homie/Hermes
continuity, memory, tools, security rules, and delegated work regardless of
whether the current turn was typed or spoken.

Smoke remains the final authority for consequential choices, credentials,
spend, external publication, irreversible changes, merges, production
activation, and mutating-tool approval. A voice surface does not weaken or
replace those boundaries.

## 5. North-star experience

Smoke joins or summons an authorized voice surface connected to the exact
active Hermes conversation. The HUD shows **Listening**.

He says:

> “Use computer control to open my email, then delegate an agent to summarize
> what needs attention.”

The finalized utterance enters the same canonical turn path as typed input.
Hermes—not the realtime provider—decides whether and how to use
`computer_use`, email tooling, and `delegate_task`. Existing approval policy is
preserved. Tool activity and background work appear through normal Hermes
receipts and durable state.

For the MVP, Hermes speaks only the exact persisted final canonical response
through native realtime audio. The spoken response is causally bound to that
Hermes assistant message; the provider cannot invent a second authoritative
answer. Spoken progress is deferred until Hermes has a separately specified,
auditable non-final output contract.

While Hermes is speaking, Smoke says:

> “Stop—wrong inbox. Use the business account.”

Only the active spoken response and its playback lease are interrupted. The
canonical Hermes session and unrelated background agents continue. The
replacement utterance is preserved and admitted exactly once as the next
serialized Hermes turn.

Smoke can then type in the same conversation and continue from the exact state
created by the voice turns.

## 6. Product promises

When the operator speaks through an authorized HUD or voice surface:

1. **Same session:** the utterance reaches the exact active profile, durable
   session, and canonical Hermes conversation selected by the surface.
2. **Same brain:** the normal Hermes model loop remains the only reasoning and
   tool-selection authority.
3. **Exact-session capability invariance:** with session configuration
   unchanged, the turn uses the identical already-resolved model loop, prompt
   prefix, context, tools, skills, plugins, memory, policy, approvals,
   delegation, Kanban, and coding workflows as typed input. Voice neither adds
   nor removes capability.
4. **Same safety:** operator identity, platform policy, approval requirements,
   consume-once authorization, and audit behavior remain unchanged.
5. **Same history:** accepted user turns, tool activity, and assistant results
   are persisted once in normal Hermes history.
6. **Native voice:** the exact persisted final canonical assistant output is rendered through
   the configured realtime provider voice, not silently routed through generic
   TTS or an audio attachment.
7. **Natural interruption:** barge-in stops the exact current response and
   playback generation without cancelling the session or unrelated work.
8. **Truthful state:** the surface reports only states proven by the canonical
   turn, provider, and exact-lease adapter/device playback lifecycle.
9. **Correct work ownership:** closing the HUD terminates transport-owned work
   but does not cancel canonical Hermes-owned tools or delegated/background work
   merely because the surface closed. Such work follows existing Hermes
   cancellation and durability semantics; process-local work is not claimed to
   survive a Hermes host restart.
10. **Reversibility:** activation, provider failure, disconnect, and rollback
    have explicit, tested recovery paths.

## 7. Jobs to be done

### 7.1 Quick assistance

- “What am I looking at?”
- “What did we decide about this?”
- “Summarize the last agent result.”
- “Remind me what is blocked.”

### 7.2 Computer and browser operation

- Open, inspect, and operate applications.
- Navigate browser workflows through normal computer/browser controls.
- Return visual or textual receipts through the canonical Hermes response.
- Preserve approvals for sensitive or mutating actions.

### 7.3 Work orchestration

- Use skills and plugins.
- Create, inspect, or update Kanban work.
- Delegate research, coding, review, or operational work.
- Launch and steer Codex, Claude Code, or other configured coding agents.
- Monitor, redirect, or stop background work without losing the voice session.

### 7.4 Business operation

- Read or draft email and calendar work.
- Prepare documents, spreadsheets, presentations, and sales assets.
- Update authorized external systems.
- Ask for progress while continuing another task on the computer.

### 7.5 Ambient continuity

- Summon or dismiss the HUD without changing conversations.
- Move between voice and text in the same session.
- Survive temporary audio transport failure without corrupting canonical
  history.
- Resume after reconnect with explicit surface/session identity.

## 8. Experience requirements

### 8.1 Entry and attachment

- Manual start or push-to-talk is the dependable first entry point.
- Wake phrase is later, optional, and must not be required for the core product.
- Attachment must bind to an exact profile, session, surface, operator, and
  transport generation.
- The HUD should remain compact, visible, and non-disruptive over the current
  application.
- Background input cannot silently attach to another conversation.

### 8.2 Listening and transcript admission

- Partial transcripts are visual and non-authoritative.
- A finalized utterance is admissible only when the Hermes host binds its
  adapter-authenticated platform principal to the authorized operator, exact
  profile/session/surface, and current transport generation.
- Only one such finalized, authorized utterance becomes one canonical Hermes
  user turn.
- Duplicate, stale, foreign-speaker, wrong-session, and replayed provider events
  fail closed before canonical admission.
- Provider attribution, transcript content, correlation IDs, and voiceprints
  confer no authority. Provider metadata is correlation data only.

### 8.3 Thinking and tool use

- Hermes executes the same canonical turn path used for typed input.
- The realtime provider receives no Hermes tool schemas and cannot dispatch
  tools.
- Tools, skills, approvals, memory, delegation, and persistence remain owned by
  Hermes.
- MVP voice renders no progress narration. Spoken progress is out of MVP until
  Hermes-authored, explicitly typed non-final lifecycle output has an auditable
  authority contract and cannot claim approval, tool success, or finality.

### 8.4 Speaking

- MVP spoken content is only the exact persisted final canonical Hermes
  assistant result.
- Native output is reserved before any generic TTS path can claim the turn.
- The configured realtime voice renders the canonical text with tools disabled.
- Exact-lease adapter/device playback-drain acknowledgment—not merely provider
  generation—determines deterministic speaking completion. Audible output is a
  separate human-confirmed canary observation.
- Native failure remains visible and never silently falls back to a different
  voice.

### 8.5 Barge-in

- A real provider speech-start event triggers interruption; timers and guesses
  do not.
- Interruption targets the exact active provider response and playback lease.
- Unrelated audio, canonical session state, tools, and background agents remain
  intact.
- The previous canonical assistant turn must already be durably terminal.
- Delivery then terminates by exactly one mutually exclusive outcome: normal
  exact-lease adapter/device drain, or host-observed interruption/cancellation
  acknowledgment for the exact provider response and playback lease.
- `INTERRUPTED` can never emit `COMPLETED`. After that interruption barrier,
  replacement speech remains pinned and is admitted exactly once as the next
  serialized user turn.
- Speech finalized while a canonical turn is still in flight but no playback
  lease exists remains pinned until that turn is durably terminal, then is
  admitted exactly once as the next serialized user turn; it is never injected
  into the active turn.

### 8.6 Approvals

- Existing visual/text approval interfaces remain authoritative in the first
  release.
- Spoken confirmation may be added later only if it resolves an existing exact,
  consume-once approval record for the authenticated operator.
- The realtime model cannot create, reinterpret, or approve an action.

### 8.7 Truthful HUD state

The surface may show only states backed by authoritative evidence:

`DISCONNECTED → CONNECTING → LISTENING → ADMITTED → THINKING → USING_TOOL /
AWAITING_APPROVAL → SPEAKING → COMPLETED`

Terminal alternatives include `FAILED` and `INTERRUPTED`. `RECONNECTING` is a
transitional recovery state and cannot itself be reported as terminal.

- **ADMITTED** requires canonical transcript acceptance.
- **THINKING** requires the canonical Hermes turn lease.
- **USING_TOOL** and **AWAITING_APPROVAL** come from Hermes, not provider text.
- **SPEAKING** requires native-output ownership plus active provider/playback
  work.
- **COMPLETED** requires canonical persistence and every required exact-lease
  adapter/device drain acknowledgment.
- **INTERRUPTED** requires exact-response cancellation plus exact-playback-lease
  interruption acknowledgment and is mutually exclusive with `COMPLETED`.

## 9. Authority model

| Concern | Sole authority |
|---|---|
| Conversation/profile/session identity | Hermes host |
| Operator and speaker authorization | Hermes host + platform adapter |
| Reasoning and model loop | Canonical Hermes session |
| Tool schemas and tool selection | Canonical Hermes session |
| Tool execution and approvals | Hermes tool/approval system |
| Memory and durable history | Hermes session and configured memory providers |
| Delegation, Kanban, cron, coding agents | Existing Hermes capabilities |
| Turn admission and serialization | Hermes host |
| Realtime audio/transcription events | Realtime provider, as untrusted input |
| Microphone capture and physical playback | Active surface/adapter |
| Native speech rendering | Realtime provider under host-issued request |
| Completion and user-visible receipts | Hermes host from combined lifecycle evidence |

No correlation ID, transcript, provider event, UI state, or spoken phrase may
cross one of these boundaries and become authority by itself.

### 9.1 Security and privacy boundary

- Capture starts only after explicit operator activation and remains visibly
  indicated while active; the host provides an immediate mute/detach path.
- Surface membership and each admitted speaker principal are authenticated and
  authorized for the exact attachment. Presence in a shared channel alone is
  insufficient.
- The realtime provider receives only the minimum bounded microphone audio,
  transcript request, and canonical render text needed for the active turn. It
  receives no Hermes credentials, tool schemas, approval records, session
  history, memory corpus, or unrelated context.
- Audio, transcripts, canonical render text, logs, and receipts have an
  explicit retention and deletion policy. Logs and receipts are redacted and
  never contain credentials, secret values, or unnecessary sensitive content.
- Shared-surface output is treated as disclosure to every authorized listener.
  Privileged or sensitive output must fail closed or require an appropriate
  private surface/explicit operator decision.
- The minimum canary uses non-sensitive data and includes negative tests proving
  an unauthorized participant can neither create a canonical turn nor receive
  a privileged response through the attachment.

## 10. Scope

### 10.1 MVP authority/transport proof

The first bounded proof uses Discord to validate the authority and native
transport contract. It is not yet the Desktop HUD release. It includes:

- one authorized Discord voice attachment to an already-running canonical
  Hermes session;
- finalized speech admitted as one canonical user turn;
- exact-session capability invariance through the normal turn path;
- native `gpt-realtime-2.1` speech with configured `cedar` voice for the exact
  canonical response;
- adapter-owned bounded PCM playback;
- exact-response barge-in with replacement-turn preservation;
- truthful state and failure receipts;
- reversible local activation and rollback;
- one real tool-backed canary after all code, review, and activation gates pass.

### 10.2 Product surfaces after the authority/transport proof

After the MVP proves the shared contract:

1. Desktop Quick Entry / native HUD;
2. Desktop main conversation voice;
3. terminal voice surfaces where transport semantics fit;
4. additional messaging/voice adapters through the same provider-neutral host
   contract;
5. optional wake phrase and spoken approval, each separately gated.

The shared authority model must remain the same across every surface. Desktop
and Quick Entry are not prerequisites for the bounded Discord canary; they are
required before declaring the complete cross-surface HUD product done.

## 11. Explicit non-goals

- A parallel “voice agent” with its own prompt, memory, transcript, or tool
  executor.
- Giving OpenAI Realtime, Gemini Live, or another provider direct Hermes tools.
- Copying Hermes tools into a fixed voice-plugin registry.
- Letting provider automatic responses become canonical Hermes replies.
- Treating legacy Hermes Talk's provider-owned conversation as native HUD
  parity.
- Cancelling the Hermes session or unrelated agents when speech is interrupted.
- Silent fallback to Edge, ElevenLabs, attachments, or another generic TTS
  provider when native output fails.
- Claiming completion when audio was generated but not physically drained.
- Shipping wake phrase, background ambient listening, or voice-only approvals
  before manual authenticated operation is reliable.
- Broad core changes when a provider-neutral contract, adapter seam, plugin, or
  external consumer is sufficient.
- Merge, publication, installation, restart, or live activation without an
  explicit separately verified gate.

## 12. Success criteria

The Discord MVP authority/transport proof is successful only when all of the
following are proven:

1. A spoken operator utterance reaches the exact already-active Hermes session.
2. The realtime provider receives zero Hermes tools and performs zero tool
   calls.
3. For the unchanged attached session, voice uses the identical already-resolved
   prompt/context, model loop, tools, policy, approvals, and persistence path as
   typed input; it neither adds nor removes capability.
4. Hermes can select and execute a deterministic, harmless, read-only dynamic
   tool through existing authentication and approval policy.
5. Hermes can start bounded non-mutating delegated work through the existing
   `delegate_task` path and observe that it continues when the voice surface
   closes or playback is interrupted.
6. The user turn, tool activity, and assistant result are persisted exactly once
   in canonical history.
7. The exact canonical assistant result is rendered through native configured
   realtime voice and played through the owning adapter.
8. Generic TTS, `.ogg`/audio attachments, and second-session executors are not
   invoked for the native turn.
9. Barge-in produces exact-response and exact-lease interruption acknowledgment,
   never `COMPLETED`, and admits the pinned correction exactly once after the
   prior canonical assistant turn is durably terminal.
10. Surface close/disconnect terminates capture, provider response, audio queue,
    playback lease, and reconnect work. Canonical Hermes-owned work is not
    cancelled merely because the surface closed and follows existing Hermes
    cancellation/durability semantics; no host-restart survival is implied for
    process-local delegation.
11. Unauthorized, stale, replayed, foreign-speaker, wrong-session,
    wrong-generation, and
    duplicated events fail closed.
12. Provider failure, playback failure, queue exhaustion, reconnect, and close
    leave no transport-owned task, queue, lease, or false completion receipt.
13. A text turn immediately after the canary continues from the same durable
    conversation state.
14. Rollback restores the exact prior reviewed runtime from a fresh process,
    preserves typed/session/history behavior, disables native routing, and
    leaves no realtime capture/provider/playback resource.

## 13. Minimum acceptance demonstration

After deterministic tests and independent review pass, the bounded live canary
must use non-sensitive data and demonstrate this sequence:

1. Smoke attaches Discord voice to the exact current Hermes conversation.
2. An unauthorized platform principal attempts a turn; the host proves no
   canonical row or Hermes turn starts. Stale, replayed, and duplicated events
   from the authorized principal are rejected the same way.
3. Smoke requests a deterministic, harmless, read-only dynamic Hermes tool.
4. If the already-resolved session policy normally requires approval, Hermes
   exercises that real approval ceremony before executing. If the selected
   harmless read-only tool requires no approval, deterministic tests separately
   prove `AWAITING_APPROVAL` ownership/preservation and the canary records that
   no live approval was required.
5. OpenAI Realtime performs no tool selection or tool execution.
6. Hermes persists the canonical result and speaks it through native
   `gpt-realtime-2.1 / cedar` audio.
7. During speech, Smoke gives a harmless correction to the read-only request.
8. The exact old response stops with `INTERRUPTED`, never `COMPLETED`; the pinned
   replacement reaches Hermes once after the prior canonical turn is durably
   terminal.
9. In a separate bounded step, Smoke delegates one non-mutating task and proves
   surface close/barge-in does not cancel it merely because the transport ended.
10. Smoke types a follow-up in the same channel/session and Hermes continues from
   the same state.
11. Logs and receipts prove no Edge TTS, audio attachment, parallel agent, replay,
   leaked task, or false drain/completion.
12. After the canary, the operator detaches native routing, restores the exact
    prior reviewed runtime/source mapping, starts a fresh process, verifies
    ordinary typed behavior and canonical history, and proves no realtime
    capture/provider/playback resource remains. This receipt does not authorize
    merge or production activation.

Email, account switching, mutating computer control, and sensitive-data demos
are optional operator-approved follow-ups, not minimum acceptance gates.

## 14. Product metrics

### 14.1 Parity and correctness

- 100% of accepted voice turns bind to one exact canonical session and
  persistence identity.
- 0 provider-originated Hermes tool calls.
- 0 duplicate canonical admissions or assistant persistence rows.
- 0 silent generic-TTS fallbacks on native-owned turns.
- 0 false completion receipts before exact-lease adapter/device drain.
- 0 cross-session, cross-speaker, or stale-generation admissions.

### 14.2 Experience

- HUD state matches actual lifecycle state during tests and canaries.
- Voice-to-text continuation requires no manual session recovery.
- The first canary treats latency as observational, not a pass/fail claim. It
  records sample count, failures, and distributions for finalized-transcript to
  canonical admission, canonical-final to first adapter audio, provider
  speech-start to exact-lease interruption acknowledgment, and reconnect.
- The realtime experience cannot be declared product-successful until the
  operator approves explicit budgets, percentile, sample count, and failure
  treatment from real evidence.

### 14.3 Reliability

- Disconnect and reconnect are recoverable without history corruption.
- Close and cancellation leave zero transport-owned capture/provider/audio/
  playback/reconnect tasks, queues, or leases. They do not redefine canonical
  Hermes work ownership.
- Rollback restores the prior reviewed runtime from an external operator-owned
  path in a fresh process, preserves typed session/history behavior, disables
  native routing, and leaves no realtime transport resource.

## 15. Delivery principles

1. **Canonical path first:** prove spoken input reaches real Hermes and can use a
   real dynamic tool before polishing audio behavior.
2. **Native output second:** bind the persisted canonical assistant response to
   provider audio and adapter playback without creating another response
   authority.
3. **Barge-in third:** interruption is safe only after exact response and
   playback ownership exist.
4. **One bounded slice per PR:** provider contract, provider adapter, host
   lifecycle, adapter playback, delivery integration, and surface UX remain
   independently reviewable where practical.
5. **Strict RED → GREEN:** authority, replay, cancellation, close, and
   cross-generation behavior are tests before implementation.
6. **Cross-repository evidence:** Agent and Talk candidates are tested together
   at exact immutable commits.
7. **Independent review:** one adversarial spec/authority review, remediation,
   then one quality/security/concurrency review of exact final diffs.
8. **No self-certification:** unit tests do not substitute for a real production
   activation seam, and fake-provider canaries do not substitute for the
   bounded native canary.
9. **Reversible activation:** exact SHAs, source mappings, process ownership,
   rollback commands, and health proof are recorded before restart.
10. **No merge without direction:** clean branches and green CI do not authorize
    merge, install, publication, or activation.

## 16. Rollout gates

### Gate A — Canonical ingress and tool parity

- Spoken transcript enters exact active session.
- The unchanged attached session uses the identical already-resolved prompt,
  context, model loop, tools, policy, approvals, and persistence path as typed
  input; voice neither adds nor removes capability.
- Provider receives no tools.
- Fake-provider parity canary passes.

### Gate B — Native canonical output

- Host-issued response binds exact persisted assistant result.
- Provider renders with tools disabled.
- Adapter-owned PCM playback drains truthfully.
- Generic TTS is suppressed before it can claim the native turn.

### Gate C — Barge-in and lifecycle safety

- Exact response cancellation and playback interruption pass race tests.
- Replacement turn survives and is admitted once.
- Background work and session state survive.

### Gate D — Surface integration

- Discord HUD/voice state is truthful.
- Failure and reconnect UX are visible and recoverable.
- This gate authorizes the bounded Discord canary only. Desktop/Quick Entry must
  later consume the same host contract without a new authority model before the
  complete cross-surface HUD product can be declared done.

### Gate E — Activation and live proof

- Exact reviewed commits and rollback receipt are recorded.
- CI and cross-repository tests are green.
- Reversible activation is verified from a fresh process.
- The bounded native canary passes with operator confirmation.
- Post-canary rollback restores the exact prior reviewed runtime in a fresh
  process, preserves typed session/history behavior, disables native routing,
  and leaves no realtime transport resource.

## 17. Risks and mitigations

### Parallel-agent drift

**Risk:** The realtime provider sounds like Hermes but owns its own dialogue and
fixed tool set.

**Mitigation:** zero provider tools, automatic responses disabled, canonical
Hermes owns every accepted turn and authoritative response.

### False voice parity

**Risk:** Generic TTS is mistaken for native realtime output.

**Mitigation:** native ownership is reserved before generic TTS; receipts name
the provider response and exact-lease adapter/device drain acknowledgment, with
human-confirmed audibility only in the bounded canary; no silent fallback.

### Barge-in data loss

**Risk:** Interruption drops the correction or cancels unrelated work.

**Mitigation:** exact response/playback generation fencing, pinned replacement
turn, canonical serialization barrier, and independent background-work tests.

### Authority from provider metadata

**Risk:** Correlation IDs or transcript events are treated as authenticated
identity.

**Mitigation:** host-owned opaque bindings and consume-once admission; provider
metadata remains non-authoritative.

### Lifecycle races and resource leaks

**Risk:** close, reconnect, cancellation, or queue pressure leaves orphan tasks,
audio, or false completion.

**Mitigation:** bounded ledgers and queues, retained ownership for transport work
until its terminal cleanup, explicit terminal states, exact-lease adapter/device
drain acknowledgments, and adversarial race tests. Surface cleanup never
redefines ownership of canonical Hermes tools or delegated work.

### Core bloat and upstream rejection

**Risk:** a product-specific implementation grows Hermes core or duplicates
plugin infrastructure.

**Mitigation:** keep core contracts provider-neutral and minimal; keep OpenAI and
Discord implementation at external/plugin/adapter edges; upstream only broadly
reusable seams backed by a real consumer.

### Accidental production activation

**Risk:** a local source mapping or restart is treated as a completed canary.

**Mitigation:** separate implementation, publication, install, restart, and
canary gates; exact receipts; operator-owned external activation; explicit
rollback.

## 18. Open product decisions

These decisions are intentionally deferred until the core canary produces real
evidence:

1. Whether a future Hermes-authored, typed, auditable non-final lifecycle output
   contract can safely support spoken progress. MVP has no spoken progress.
2. Whether the text transcript is always mirrored while native audio plays.
3. The latency targets for admission, first audio, interruption, and reconnect.
4. Whether background speech can continue after the HUD loses visual focus.
5. Which surfaces may support spoken approval and what anti-replay ceremony is
   required.
6. Whether wake phrase belongs in Hermes core UX, a Desktop feature, or an
   external transport plugin.
7. Which realtime providers qualify after OpenAI proves the provider-neutral
   contract.

None of these may weaken the authority model in Section 9.

## 19. Current evidence and starting point

At this PRD revision, the following are unverified starting hypotheses unless an
exact immutable commit and receipt are attached by the implementation program:

- Quick Entry appears to route submitted text through normal selected-session
  machinery rather than owning another gateway connection.
- Hermes appears to have provider-neutral realtime voice contract/controller
  work in progress.
- The Discord core-session attachment has supplied input-only direction and
  local evidence, but input-only admission proves neither output authority,
  replay resistance, interruption safety, security/privacy completeness, nor
  production readiness.
- Hermes Talk remains a valuable duplex transport and provider adapter, but its
  legacy provider-owned conversation and fixed tool registry are not the
  target HUD authority model.
- Native host-controlled response, adapter-owned Discord PCM playback,
  interruption, authoritative delivery integration, and the complete
  production canary remain gated work.

Every current-state claim must be replaced by an exact immutable commit, scoped
test/receipt, and explicit limitation before it can be used as a rollout gate.
Source and implementation documents existing in the directory are not proof
that behavior is deployed.

## 20. Definition of done

Hermes Native Realtime HUD is done only when Smoke can naturally speak to the
same already-running Hermes that he types to; for that exact unchanged session,
voice uses the identical already-resolved prompt/context, model loop, tools,
policy, approvals, memory, delegation, Kanban, coding-agent capabilities, and
persistence path as typed input; the canonical response is spoken through
native realtime audio; Smoke can interrupt and redirect it without losing the
session or cancelling canonical Hermes-owned work merely because the surface
closed; every accepted turn and terminal delivery outcome is durably and
truthfully evidenced; the security/privacy boundary is proven; and the system
can be reverted cleanly.

Anything less is a useful milestone, not full HUD parity.
