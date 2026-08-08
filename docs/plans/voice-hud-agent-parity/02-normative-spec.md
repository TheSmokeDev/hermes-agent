# Voice HUD Agent Parity — Normative Architecture Specification

**Status:** Draft
**Base:** Hermes Agent `155f05266d6b67ff008966397875ac356bcf9890`; Hermes Talk `09085c7536ffa17199da704d25192732cf530a68`

Normative terms **MUST**, **SHALL**, **SHOULD**, and **MAY** are binding for this epic.

## Authority model

- **VH-001 Same agent:** Voice SHALL be an input/output transport for an existing Hermes session, not a separate general-purpose agent.
- **VH-002 Host authority:** The owning Hermes surface SHALL remain authoritative for principal identity, active profile, routing key, canonical runtime/durable session identities, memory policy, tool availability, approvals, persistence, and finalization.
- **VH-003 Provider boundary:** Realtime provider events, `provider_data`, transcript roles, display names, arguments, and echoed metadata SHALL NOT grant operator identity or mutation authority.
- **VH-004 No parallel executor:** Provider-originated tool calls SHALL NOT invoke `PluginContext.dispatch_tool`, `ToolRegistry.dispatch`, Talk's custom executor, or `handle_function_call` directly. General work SHALL enter the canonical Hermes user-turn path.
- **VH-005 Frozen capabilities:** Voice attachment SHALL NOT mutate a live session's system prompt, prior messages, or frozen tool snapshot. Material capability changes require a new Hermes session.
- **VH-006 Same policy:** A voice-originated turn SHALL use the same agent, tool schemas, middleware, approvals, guardrails, hooks, delegation/Kanban context, computer-use approval namespace, accounting, persistence, and finalization as typed input.

## Identity and admission

- **VH-007 Identity tuple:** Voice controllers SHALL bind profile, routing key, runtime session ID, durable session ID, and provider session ID as distinct fields. No unqualified `session_id` conversion is permitted.
- **VH-008 Final-only admission:** Partial transcripts MAY update captions but SHALL NOT become user messages. Only a final transcript with a host-issued consume-once permit MAY enter the agent input lane.
- **VH-009 Replay safety:** Admission SHALL deduplicate final utterances by stable provider-session/turn/item identity using bounded storage. Rejected, stale, malformed, unauthorized, cancelled, and teardown-discarded attempts consume or revoke authority and SHALL NOT replay.
- **VH-010 Serialization:** Voice utterances SHALL enter the same serialized input/turn-lease path as text. A voice bridge SHALL NOT call `AIAgent.run_conversation` concurrently with another turn or write transcript rows directly.
- **VH-011 Shared rooms:** Surface adapters MAY collect immutable participant identity. Only surface policy may map that identity to a permit. Discord mutations remain limited to configured immutable operator IDs by default.

## Lifecycle and events

- **VH-012 State machine:** A voice session SHALL expose one deterministic lifecycle: disconnected → connecting → ready/listening → queued/thinking/acting/speaking → closing → closed, with failed/reconnecting branches and one terminal event.
- **VH-013 Durable truth:** UI progress is provisional. `agent.completed` SHALL be emitted only after canonical turn finalization; tool completion SHALL correspond to the canonical executor result, not provider acknowledgement.
- **VH-014 Barge-in:** Interruption SHALL stop output first. Replacement speech becomes a new serialized turn only after the interrupted turn reaches its persistence/release boundary.
- **VH-015 Backpressure:** Audio, transcript, and admission queues SHALL be bounded. Overflow fails visibly and SHALL NOT block provider event consumption indefinitely.
- **VH-016 Teardown:** Stop admission, revoke unconsumed permits, suppress late callbacks, stop capture/playback, close the provider idempotently, and emit one terminal event. Closing voice SHALL NOT implicitly cancel background work.
- **VH-017 Presence truth:** Inactivity timeout, voice-channel departure, provider failure, and permission denial SHALL be surfaced in the HUD/chat. The product SHALL NOT silently continue as if live voice remains connected.

## Desktop and gateway boundaries

- **VH-018 One connection:** Quick Entry SHALL continue forwarding through the primary renderer; it SHALL NOT open its own gateway or provider connection.
- **VH-019 Renderer boundary:** The renderer SHALL NOT receive provider secrets or execute tools. Electron owns native permission/window concerns; the backend owns agent work and backend-authoritative state.
- **VH-020 Current-session MVP:** The first HUD voice action SHALL support only `target:'current'` and use the existing main-composer voice-start path. It SHALL require an already-bound runtime session, carry a host snapshot of active profile/runtime/durable identity, reject a stale latch, and stop before transcript submission on identity drift. Gateway-open blank/new drafts and background sessions remain text-only.
- **VH-021 Capability truth:** Realtime controls SHALL be enabled only when a usable registered provider and required capabilities are available. Older backends SHALL retain the existing turn-based voice path and label it accurately.
- **VH-022 State reuse:** Tool, approval, subagent, run, and session truth SHOULD reuse canonical Desktop stores rather than voice-specific copies.

## Provider and Talk ownership

- **VH-023 Core:** Core owns the provider ABC/registry, normalized IDs/events, same-session admission contract, turn serialization, policy/execution path, persistence, and gateway session/event contract.
- **VH-024 Talk:** Talk remains a standalone plugin and owns OpenAI Realtime wire translation, credentials, model/voice/audio configuration, terminal audio, Discord PCM/speaker attribution, and voice-specific diagnostics/presentation.
- **VH-025 Migration:** Talk SHALL implement/register the core provider API before its fixed tool executor is retired. Compatibility lanes MAY remain explicitly capability-gated and SHALL report degraded status truthfully.
- **VH-026 Desktop:** Desktop owns the compact HUD controls and projections; it consumes the backend contract and canonical stores.

## Required host bridge

A provider-neutral same-session bridge SHALL define equivalents of:

```python
@dataclass(frozen=True)
class RealtimeUtterance:
    provider_session_id: str
    item_id: str
    provider_turn_id: str
    text: str
    received_at: float

class RealtimeInputAuthorizer(Protocol):
    async def authorize(self, event: InputTranscript) -> HostUtterancePermit | None: ...

class SameSessionTurnIngress(Protocol):
    async def submit(
        self, utterance: RealtimeUtterance, permit: HostUtterancePermit
    ) -> AgentTurnReceipt: ...
```

The opaque permit is host-created and internally bound to principal/profile/routing/session identities. It is never serialized to the provider.

## Conformance proof

A release candidate SHALL demonstrate with a fake provider and temporary `HERMES_HOME`:

1. one operator transcript enters the existing session exactly once;
2. text and voice expose equivalent canonical tools/policy;
3. computer use and an approval round trip remain session-isolated;
4. tool/subagent events and persisted transcript agree on turn/batch/call identities;
5. stale profile/session events fail closed;
6. interruption, inactivity, provider failure, reconnect, and repeated close leave no microphone/playback/session leak;
7. participant speech cannot mint operator authority;
8. background work survives voice close unless explicitly cancelled.
