# Voice HUD Agent Parity — Intent / PRD

**Status:** Draft for one adversarial review
**Epic:** Voice HUD Agent Parity
**Product owner:** SmokeDev
**Repositories:** `NousResearch/hermes-agent` + standalone `TheSmokeDev/hermes-talk`

## Intent

Hermes should stop feeling like a window the operator must switch into. It should become an always-available agent layer over whatever app the operator is already using: summon the Desktop Quick Entry HUD, speak naturally, and ask the same Hermes session to think, use tools, operate the computer, run skills, manage Kanban, delegate agents, or answer a quick question.

The voice surface is not a second agent. It is another input/output transport for the active Hermes session.

## Product promise

When the operator speaks through the HUD or an authorized voice surface:

- the utterance reaches the same active profile and Hermes conversation as typed input;
- the normal Hermes agent remains the brain and authority;
- the existing live toolset, skills, memory, approvals, computer-use policy, delegation limits, Kanban role, persistence, and receipts remain unchanged;
- the HUD stays compact and visible over the operator's current application;
- Hermes reports truthful state: disconnected, connecting, listening, queued, thinking, using a tool, awaiting approval, speaking, completed, failed, or reconnecting;
- long work may continue after the voice surface closes, but no new mutation may be attributed to voice without current authority.

## Primary jobs

1. **Quick assistance:** “What am I looking at?” “Remind me what we decided.”
2. **Computer operation:** open and inspect apps, operate browser workflows, and return visual receipts.
3. **Work orchestration:** use skills, create/manage Kanban work, delegate Codex/Claude/subagents, and monitor or redirect them.
4. **Business operation:** draft or publish content, manage email and calendars, prepare workbooks/documents, and update systems through the same normal approval path.
5. **Ambient continuity:** summon or dismiss the HUD without losing the active Hermes session.

## Experience

### Summon

- Global Quick Entry shortcut remains the first dependable entry point.
- Wake phrase is a later opt-in; push-to-talk/manual start must work without it.
- The HUD never steals focus unnecessarily and does not create another gateway connection.

### Conversation

- Partial captions are visual only.
- A finalized authorized utterance becomes one canonical Hermes user turn.
- Barge-in stops audio/output first; replacement speech enters as a new serialized user turn after the previous turn reaches its persistence boundary.

### Work and approvals

- Voice can request anything text Hermes can request.
- Hermes—not the realtime provider—chooses and executes tools.
- Mutating and sensitive operations retain the normal approval interface. Spoken confirmation may be added only when it resolves to the same consume-once approval record.
- Background agents survive HUD close unless the operator explicitly cancels them.

## Delivery strategy

Ship value in two compatible steps:

1. **Full-capability HUD MVP:** Quick Entry starts the existing Desktop voice conversation in the current session. It is turn-based STT → normal Hermes turn → TTS, but immediately inherits full text-Hermes capabilities.
2. **Realtime transport upgrade:** attach provider-neutral duplex audio to the same canonical turn ingress and backend-owned session without changing the HUD's authority or session model.

The MVP is not marketed as native realtime. The realtime upgrade must not create a parallel tool executor.

## Success criteria

- The operator can summon Quick Entry, start voice, and complete a computer-use or skill-backed task in the already-active session.
- The same task has the same tool availability, approval requirement, persisted transcript, and receipt whether initiated by text or voice.
- Switching profile/session or receiving stale provider events cannot redirect a voice turn.
- Voice disconnect, inactivity timeout, provider failure, and microphone denial are visible and recoverable.
- No raw provider credentials or general-purpose tool execution move into the renderer.

## Non-goals

- A separate “voice agent” with its own memory, prompt, or hardcoded copy of Hermes tools.
- Giving a realtime provider direct authority to dispatch tools.
- Background-session voice in the first HUD PR.
- Shipping a wake phrase before manual HUD voice is reliable.
- Replacing Discord/terminal Talk transport code before the core session bridge has a real consumer.
- Merging or publishing any branch without explicit approval.

## Current evidence

- Quick Entry intentionally has no gateway and forwards `{target,text}` to the primary renderer: `apps/desktop/electron/quick-entry.ts`, `apps/desktop/electron/main.ts`, `apps/desktop/src/store/quick-entry.ts`.
- `useQuickEntryBridge()` already routes current/new/stored targets through the normal session submission machinery: `apps/desktop/src/app/contrib/hooks/use-quick-entry-bridge.ts`.
- Desktop already has a main-composer voice start and turn-based voice loop: `apps/desktop/src/store/composer.ts`, `use-composer-voice.ts`, `use-voice-conversation.ts`.
- Core's provider contract explicitly reserves identity, memory, authorization, and tools to the host: `agent/realtime_voice_provider.py`.
- Core's current orchestrator only pumps normalized events; it does not own agent turns or tools: `agent/realtime_voice_orchestrator.py`.
- Talk currently exposes a fixed tool subset and a parallel executor: `talk_tools.py`, `talk_relay.py`, `talk_cli.py`.
