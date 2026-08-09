# PRP-006A — Discord Core-Session Attachment and Operator Canary

> **For Hermes:** Use subagent-driven development and strict RED → GREEN. This PRP receives one adversarial architecture review; after its concrete blockers are incorporated, move review to executable evidence and the final diff.

**Status:** Implementation in progress after one adversarial review. The review narrowed this to an explicit, input-only, idle-session Discord canary; live activation remains gated on green cross-repository tests and final diff review.

**Agent base:** `40b2a05e65504d7442611a8238bab1d30fae8dfe` (`feat/realtime-gateway-controller`)
**Talk base:** `28d7068521b3e3e4d2e37a17220a5698455d86aa` (`main`)
**Depends on:** PRP-002 final-transcript admission, PRP-003 controller, PRP-004 registered input-only Talk provider.

**Goal:** Add an explicit `/talk core join` canary that uses an invocation-scoped, host-issued attachment to admit Smoke's immutable Discord voice input into the exact canonical Hermes gateway session. Keep legacy `/talk join` and standalone `hermes talk` accurately labeled and unchanged until the canary is proven.

**Architecture:** The gateway owns session identity, authority, canonical turn execution, persistence, tools, approvals, and response delivery. Talk owns only Discord PCM capture and the registered input-only provider. A host-minted, non-serializable attachment capability is captured while the authenticated `/talk` command is dispatched, pinned to the exact source/routing key/durable session and invocation principal, and consumed through the existing gateway message/turn-lease path. The provider receives no Hermes tools and cannot execute work.

**Tech stack:** Python 3.11+, asyncio, existing gateway `MessageEvent` ingress and `SessionTurnLeaseRegistry`, `GatewayRealtimeVoiceController`, Talk API-v2 input provider, `DiscordAudio`, pytest/pytest-asyncio.

---

## Scope boundary

PRP-006A is the smallest safe Discord proof. It does **not** claim full Talk convergence.

In scope:

- Explicit contextual slash-command registration; legacy one-argument handlers receive no voice authority.
- A host-minted immutable invocation object whose narrow realtime service can capture one exact attachment factory only during the authenticated Discord invocation.
- A concrete messaging-gateway realtime host that admits one final transcript through the installed canonical `_handle_message` path.
- Exact durable-session/routing/principal proof, consume-once permits, visible busy-session rejection, and accepted-work survival.
- Talk Discord core adapter using the registered `talk_openai_realtime` input-only provider.
- Operator-only audio for the first canary: the Agent attachment independently enforces that only PCM carrying the exact immutable invoking Discord user ID may reach the provider. Transport-generated silence uses a separate zero-only path and cannot smuggle content.
- Idle-session-only canonical admission. A busy route is rejected visibly; it is never interrupted, steered, merged, replaced, or treated as accepted queue work.
- A host-generated high-entropy turn marker persisted on the canonical user row and exact SessionDB read-back before completion is claimed.
- Canonical response delivery through the existing Discord text adapter, with truthful status/failure receipts.

Explicit non-goals:

- PRP-005 Desktop HUD/RPC/audio work.
- Interactive and standalone terminal migration. The former needs a `CliRealtimeTurnHost`; the latter remains blocked on a cross-process durable turn lease.
- Non-operator conversational/read-only participant turns; shared-room attribution is a later PRP-006 slice and must not be guessed from provider metadata.
- Provider-native output audio, provider-native Hermes tools, a second agent/session/executor, or legacy Talk tool dispatch.
- Replacement of legacy `/talk join`. Unsupported `/talk core join` fails visibly and never falls back to the legacy Talk executor.
- New persistence schema, provider credentials in Agent/renderer, or a live production activation before tests pass.

## Required invariants

1. Provider transcript roles, item IDs, display names, and metadata are never identity proof.
2. The gateway derives and captures source principal, routing key, current durable session, platform/thread scope, and run generation.
3. A private attachment proof cannot be serialized or replayed externally.
4. Canonical acceptance requires the exact captured SessionEntry object/generation, routing claim, and resolved-session turn lease. `/new`, `/resume`, compression rotation, source drift, route replacement, and recycled public IDs fail closed before acceptance.
5. The installed `_handle_message` and turn-lease path remain the only agent/tool/approval/persistence executor.
6. Provider tool events remain inert. Talk core setup contains no tools and `automatic_response=False`.
7. A Discord audio packet is admitted only when its immutable speaker user ID equals the host-authorized operator ID. Mixed/unknown/non-operator audio is dropped before provider admission and cannot become a canonical turn.
8. Closing voice revokes unconsumed permits and provider/audio ownership but never cancels canonically accepted work or unrelated agents.
9. Completion requires exact marker read-back followed by the canonical assistant row after normal gateway unwind. Matching text, row counts, or caller-minted receipts are insufficient.
10. Every terminal path clears attachment/pending state exactly once and emits a truthful receipt.
11. Existing text messages, ordinary plugin commands, legacy `/talk join`, terminal Talk, and turn-based voice behavior are unchanged when no private attachment is present.

## Hard file scope

### Hermes Agent

Expected production files:

- Create: `gateway/realtime_voice_messaging_host.py`
- Create: `gateway/realtime_voice_invocation.py`
- Modify: `gateway/run.py` only at plugin-command invocation, exact-session validation/claim, and teardown hooks.
- Modify: `hermes_cli/plugins.py` only to expose the invocation-scoped attachment API.
- Modify: `gateway/session_state.py` only if an exact attachment record needs a lifecycle-owned field.
- Modify: `gateway/realtime_voice_controller.py` only if a host-visible event sink is required; no executor logic.

Expected tests:

- Create: `tests/gateway/test_realtime_voice_invocation.py`
- Create: `tests/gateway/test_realtime_voice_messaging_host.py`
- Create: `tests/gateway/test_realtime_voice_messaging_integration.py`
- Modify: `tests/hermes_cli/test_plugins_realtime_voice_registration.py` or the nearest PluginContext test only for the additive invocation API.

### Hermes Talk

Expected production files:

- Create: `talk_core_session.py`
- Modify: `talk_discord.py` at `start_session`/`stop_session` and operator audio filtering only.
- Modify: `__init__.py` only to select the core path from `/talk` and fail visibly when unavailable.
- Modify: `talk_doctor.py` only for passive host-attachment readiness if needed.

Expected tests:

- Create: `tests/test_core_session.py`
- Modify: `tests/test_discord.py`
- Modify: `tests/test_register.py`

Do not modify Talk's legacy `run_talk_session()` executor in this slice. The Discord constructor must stop selecting it when the core path is requested; terminal legacy remains isolated for PRP-006B.

## RED → GREEN tasks

### Task 1 — Invocation-scoped host capability

**RED:** Prove only an explicitly contextual Discord plugin command receives a host-minted invocation service and can capture a factory. Legacy handlers still receive exactly one argument. Non-Discord, unauthenticated, outside-dispatch, post-return, foreign-source, copied, and serialized-lookalike paths fail closed. Remove/recreate under the same public IDs and a switched durable mapping are distinguishable by exact host identity.

**GREEN:** Add explicit contextual registration metadata plus an immutable host-minted invocation object around the existing plugin handler call. Capture exact runner, SessionEntry object/generation, source/principal/routing/durable authority privately. Do not place mutable current-session fields on the global PluginContext or expose host objects to plugins.

### Task 2 — Exact messaging-host permit and canonical ingress

**RED:** Prove exact binding + exact operator utterance consumes one permit and enters the real `_handle_message`/turn-lease path once. Wrong/reused/foreign permits; stale route; durable-session replacement; `/new`/`/resume` drift; recycled IDs; wrong source/platform/thread/principal; and pre-claim races fail closed. A busy route rejects visibly without interrupting, steering, merging, queueing, or direct agent re-entry. Completion requires exact durable marker read-back.

**GREEN:** Implement one messaging host that captures immutable source/routing/durable authority, validates at the actual session-resolution/claim boundary, synthesizes the normal internal `MessageEvent`, and awaits the canonical handler. Return positive acceptance/finalization only from the real canonical result and persistence boundary.

### Task 3 — Controller composition and teardown

**RED:** A fake API-v2 input provider opens once, emits one authorized final transcript, reaches the same durable session, and closes once. Partial, duplicate, participant, provider-tool, stale-generation, overflow, cancelled-open, interrupt race, and repeated-close cases never enter canonical work or leak tasks. Accepted work survives controller close.

**GREEN:** Compose the existing controller and messaging host behind the captured attachment factory. Keep provider tools inert and preserve existing controller bounds/fences.

### Task 4 — Talk Discord operator-only transport

**RED:** Prove `/talk core join` selects the core attachment path, not `talk_cli.run_talk_session`; legacy `/talk join` remains unchanged and clearly labeled. Only packets carrying the exact immutable invoker `user_id` enter the controller; unknown/non-operator/mixed/coercive packets are dropped by the Agent attachment. Zero-only synthesized silence has a separate path. Stop and bridge loss close audio/provider/attachment once. Missing host API or unavailable provider produces an explicit receipt and never silently starts legacy Talk.

**GREEN:** Add `talk_core_session.py` to build the input-only setup and feed bounded 24 kHz mono PCM from `DiscordAudio`. Preserve Discord connection borrowing/restoration and status/failure delivery.

### Task 5 — Installed cross-repository canary

Use a temporary `HERMES_HOME`, the real gateway session store, installed plugin-command dispatch, real messaging host/controller, fake Talk-compatible provider, and real persistence seam.

Prove:

1. `/talk core join` captures one exact Discord source/operator and existing durable session;
2. one final transcript persists exactly once in that same durable session;
3. the ordinary gateway agent path, tools/approvals policy, turn lease, response delivery, and persistence are used;
4. no second gateway, durable session, `AIAgent`, tool registry, executor, or legacy Talk loop is constructed;
5. duplicate, stale, non-operator, participant, and provider-tool events do not enter;
6. close after positive canonical acceptance does not cancel the blocked accepted turn;
7. every failed startup/teardown path leaves a truthful status and no live attachment.

### Task 6 — Local reversible canary gate

Only after Tasks 1–5 and final diff review pass:

- install/use the isolated Agent and Talk branches locally without replacing published releases;
- verify passive doctor/provider/Discord operator readiness without printing secrets;
- start the existing gateway and join a controlled Discord voice channel;
- activate `/talk core join` as Smoke's immutable user ID;
- speak a harmless, read-only prompt first;
- verify the exact durable session gained one user turn and canonical assistant response;
- verify `/talk leave` returns the borrowed Discord connection and no controller/provider task remains;
- do not test mutations until identity and approval receipts are visibly correct.

## Verification commands

Agent focused gates:

```bash
python -m pytest -p no:cacheprovider \
  tests/gateway/test_realtime_voice_controller.py \
  tests/gateway/test_realtime_voice_invocation.py \
  tests/gateway/test_realtime_voice_messaging_host.py \
  tests/gateway/test_realtime_voice_messaging_integration.py -q

python -m pytest -p no:cacheprovider \
  tests/gateway/test_turn_lease.py \
  tests/agent/test_realtime_voice_admission.py \
  tests/agent/test_realtime_voice_provider.py \
  tests/agent/test_realtime_voice_registry.py \
  tests/agent/test_realtime_voice_orchestrator.py -q
```

Talk focused gates:

```bash
python -m pytest -p no:cacheprovider \
  tests/test_core_realtime_adapter.py \
  tests/test_core_session.py \
  tests/test_discord.py \
  tests/test_register.py -q
```

Static/full gates:

```bash
ruff check gateway/realtime_voice_invocation.py gateway/realtime_voice_messaging_host.py \
  gateway/realtime_voice_controller.py gateway/run.py hermes_cli/plugins.py \
  tests/gateway/test_realtime_voice_invocation.py \
  tests/gateway/test_realtime_voice_messaging_host.py \
  tests/gateway/test_realtime_voice_messaging_integration.py

git diff --check
```

Run repository-prescribed full Python, formatting, lint, packaging, and Windows CI-equivalent gates in both repositories before push.

## Acceptance criteria

- [ ] Discord `/talk core join` launches only the core input lane; legacy `/talk join` remains unchanged and is never mislabeled as core-backed.
- [ ] Exactly one immutable Discord operator principal is authorized for the first canary.
- [ ] Final transcripts enter the exact existing idle gateway/durable session through canonical ingress and serialization; busy routes reject without queueing or interruption.
- [ ] Completion is proven by a host-generated marker read back from the exact durable SessionDB row followed by the canonical assistant row.
- [ ] Existing Hermes tools, approvals, persistence, memory, and response delivery remain authoritative.
- [ ] Provider tool events cannot execute tools.
- [ ] No second agent/session/gateway/executor is created.
- [ ] Binding/route/principal drift and all lifecycle races fail closed.
- [ ] Teardown is bounded, idempotent, and preserves accepted/background work.
- [ ] Focused, regression, full, lint, packaging, and diff gates pass in both repositories.
- [ ] Local canary reports exact session/persistence and teardown receipts before any parity claim.

## Backout

The new path is capability-gated and has no migration. Backout disables/removes the invocation attachment API and Talk core constructor. It must not rewrite conversations or change ordinary text/plugin behavior. Legacy terminal Talk remains separately available until PRP-006B; Discord must fail visibly rather than silently return to a parallel executor.
