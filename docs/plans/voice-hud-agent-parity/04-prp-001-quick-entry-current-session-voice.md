# PRP-001 — Quick Entry Current-Session Voice Launch

> **For Hermes:** implement with strict RED → GREEN in a fresh isolated worktree. This PRP is the first shippable slice of the Voice HUD Agent Parity epic.

**Goal:** Add a microphone action to Desktop Quick Entry that starts the existing main-composer voice conversation against the already-active Hermes session.

**Architecture:** Quick Entry remains a gateway-free mini renderer. A typed IPC intent travels Quick Entry → Electron main → primary renderer. `useQuickEntryBridge()` invokes the existing latched `requestVoiceConversationStart()` seam; the mounted main composer owns microphone capture and sends finalized transcripts through the normal `onSubmit → useSubmitPrompt → prompt.submit` path.

**Tech stack:** Electron IPC, React/TypeScript, Nanostores, Vitest/Testing Library.

## Business context

This slice gives the HUD immediate full text-Hermes capabilities—tools, skills, approvals, computer use, delegation, Kanban, persistence—without waiting for native realtime provider migration. It is accurately labeled as the existing turn-based voice transport.

## Hard scope

### Allowed production files

- `apps/desktop/electron/main.ts`
- `apps/desktop/electron/preload.ts`
- `apps/desktop/electron/quick-entry.ts`
- `apps/desktop/src/global.d.ts`
- `apps/desktop/src/store/quick-entry.ts`
- `apps/desktop/src/store/composer.ts`
- `apps/desktop/src/app/quick-entry/quick-entry-app.tsx`
- `apps/desktop/src/app/contrib/hooks/use-quick-entry-bridge.ts`
- `apps/desktop/src/app/chat/composer/hooks/use-composer-voice.ts`
- `apps/desktop/src/app/chat/composer/hooks/use-voice-conversation.ts`
- `apps/desktop/src/app/contrib/wiring.tsx` only if required to pass voice state/handler into the existing bridge

### Allowed tests

- `apps/desktop/electron/quick-entry.test.ts`
- `apps/desktop/src/store/quick-entry.test.ts`
- `apps/desktop/src/store/composer.test.ts`
- `apps/desktop/src/app/quick-entry/quick-entry-app.test.tsx` (create if absent)
- `apps/desktop/src/app/contrib/hooks/use-quick-entry-bridge.test.tsx` (create if absent)
- `apps/desktop/src/app/chat/composer/hooks/use-composer-voice.test.tsx` (create if absent)
- existing voice-conversation tests only when adding failure-projection coverage

## Explicit non-goals

- No core realtime provider/orchestrator edits.
- No new gateway RPC or WebSocket.
- No microphone capture in the Quick Entry renderer.
- No voice start for `new` or a selected stored/background session.
- No wake-word redesign, provider credentials, dynamic tools, new approval mechanism, or persistence format.
- No visual claim that this is provider-native realtime.

## Source anchors

- `apps/desktop/electron/quick-entry.ts`: Quick Entry is intentionally gateway-free.
- `apps/desktop/electron/main.ts`: existing `hermes:quick-entry:submit/state/dismiss` relay.
- `apps/desktop/src/store/quick-entry.ts`: `QuickEntrySubmitPayload`, `QuickEntryStatePush`, reducer/bridge.
- `apps/desktop/src/app/contrib/hooks/use-quick-entry-bridge.ts`: current/new/stored routing into canonical prompt submission.
- `apps/desktop/src/store/composer.ts`: `requestVoiceConversationStart()` latched intent.
- `apps/desktop/src/app/chat/composer/hooks/use-composer-voice.ts`: consumes the intent only in the main composer.
- `apps/desktop/src/app/chat/composer/hooks/use-voice-conversation.ts`: existing STT/normal prompt/TTS/barge-in loop.

## Contract

Add a dedicated intent; do not overload empty text submission.

```ts
export type QuickEntryVoiceStartPayload = {
  target: typeof QUICK_TARGET_CURRENT
}
```

The preload bridge exposes a narrow `startVoice(payload)` action. Electron main SHALL accept it only when `event.sender === quickEntryWindow.webContents`, validate `payload.target === QUICK_TARGET_CURRENT`, and relay it only to the authoritative primary renderer. Main, secondary, stale, unknown, malformed, `new`, and stored-target senders/payloads fail closed.

The primary renderer SHALL push `currentVoiceTargetAvailable` plus a minimal primary-owned voice projection (`available`, `active`, `status`, terminal `error`) in `QuickEntryStatePush`. `currentVoiceTargetAvailable` is true only when the gateway is open and `$activeSessionId` is non-null. The Quick Entry mic is disabled otherwise even when text submission remains available.

On a valid relay, the primary renderer captures a host-owned binding containing at least active profile, runtime session ID, and selected durable session ID, then calls the existing composer start seam with that binding. The renderer handler SHALL:

1. accept only the primary renderer's current-target intent;
2. call `requestVoiceConversationStart()` exactly once;
3. not call `submitText()`, `startFreshSessionDraft()`, `submitToSession()`, or `session.create`;
4. leave the main window hidden unless existing microphone/OS behavior requires otherwise;
5. surface unavailable/disconnected/start-failed state in Quick Entry rather than hiding with no feedback.

The linearization point is **successful consumption by the main composer after exact binding validation**. A stale latch is consumed/rejected and cannot start later. While active, profile/runtime/durable-session drift ends capture before another transcript can submit. `submitVoiceTurn()` validates the same binding immediately before canonical `onSubmit`; mismatch fails closed. A gateway-open blank/new draft is text-capable but voice-ineligible and SHALL NOT create a session.

## TDD plan

### Task 1 — Type the Quick Entry voice intent

1. Add failing reducer/type tests proving voice is a separate action and cannot carry `new` or stored targets.
2. Run the focused store test; confirm RED.
3. Add the minimal payload/state contract, `currentVoiceTargetAvailable`, and voice projection in `store/quick-entry.ts` and matching global/preload types.
4. Run focused tests; confirm GREEN.

### Task 2 — Relay voice through Electron

1. Add a failing Electron test proving `startVoice({ target: QUICK_TARGET_CURRENT })` from the Quick Entry sender reaches the primary renderer exactly once and does not submit text.
2. Add negative RED cases for main/secondary/unknown senders and malformed/new/stored targets.
3. Run the focused Electron test; confirm RED.
4. Add the narrow preload method, sender check, literal validation, and main-process relay.
5. Run focused tests; confirm GREEN.

### Task 3 — Route into the existing composer voice seam

1. Add a failing hook test proving a valid intent snapshots active profile/runtime/durable identity, calls `requestVoiceConversationStart(binding)` once, and does not run any text/new/stored submission path.
2. Add cases proving gateway-open with no active runtime, secondary/unmounted surfaces, and stale/malformed events cannot start voice.
3. Add composer RED cases for drift before latch consumption and drift while active/before transcript submit.
4. Run focused hook/composer tests; confirm RED.
5. Add the minimal handler, binding-aware latch, drift teardown, and state projection.
6. Run focused tests; confirm GREEN.

### Task 4 — Add the current-target microphone control

1. Add failing component tests for visible mic control, current-target-only enablement, gateway-open-but-no-runtime disabled state, disconnected/unavailable disabled state, projected lifecycle/error labels, and no empty submit.
2. Run component tests; confirm RED.
3. Implement the minimal control and deterministic status/error copy.
4. Run component tests; confirm GREEN.

### Task 5 — Regression and packaged behavior

1. Run focused Quick Entry, voice-conversation, and wake-indicator suites.
2. Run Desktop typecheck and lint.
3. Package/run a Windows smoke: summon Quick Entry, current active session selected, start voice, complete one normal Hermes request, stop voice, verify no second gateway/session was created.
4. Record exact commands, exit codes, and any platform-specific permission result.

## Exact verification commands

From repository root:

```bash
npm run test:ui --workspace apps/desktop -- \
  src/store/composer.test.ts \
  src/store/quick-entry.test.ts \
  src/app/quick-entry/quick-entry-app.test.tsx \
  src/app/contrib/hooks/use-quick-entry-bridge.test.tsx \
  src/app/chat/composer/hooks/use-voice-conversation.test.tsx \
  src/app/chat/composer/hooks/use-voice-conversation-rearm.test.tsx

npm run test:desktop:platforms --workspace apps/desktop -- \
  electron/quick-entry.test.ts \
  electron/wake-indicator.test.ts \
  electron/active-runtime-state.test.ts

npm run typecheck --workspace apps/desktop
npm run lint --workspace apps/desktop
```

These argv were verified against `apps/desktop/package.json` and executed on the clean base on 2026-08-08:

- UI baseline: 5 files, 42 tests passed (including the existing voice-start latch).
- Electron baseline: 3 files, 34 tests passed.
- Desktop typecheck: passed.
- Desktop lint: passed with 88 pre-existing warnings and 0 errors.
- One pre-existing React `act(...)` warning appeared in `use-voice-conversation.test.tsx`; it did not fail the suite.

The two new test files named above do not exist yet and are created during RED.

## Acceptance criteria

- [x] Quick Entry displays an accessible microphone action only for the current target.
- [x] Gateway-open with no active runtime remains text-capable but voice-ineligible; voice cannot create a session.
- [x] The intent crosses the narrow preload/main IPC bridge and reaches the primary renderer once.
- [x] Electron validates the Quick Entry sender and exact current-target literal; every other sender/target fails closed.
- [x] The existing main-composer voice conversation starts without creating a second gateway, provider session, Hermes session, or tool executor.
- [x] A finalized voice transcript uses the already-selected Hermes session's normal prompt submit path.
- [x] Profile/runtime/durable-session drift before consumption or during capture ends/rejects voice before submission to another conversation.
- [x] Existing approvals, tools, computer-use policy, skills, delegation/Kanban context, transcript persistence, and background work behavior remain unchanged.
- [x] New/stored targets remain text-only and are visibly unsupported for voice.
- [x] Disconnected/unavailable/start-failed state is visible; the HUD does not silently pretend voice is live.
- [x] Focused tests, typecheck, lint, and Windows packaged smoke pass with receipts.

## Backout

Revert the typed action, IPC relay, and mic control. No data or configuration migration is introduced. Existing text Quick Entry and main-composer voice remain intact.

## Completion evidence

Implementation revision: `e5b946b7595d34d10c1b7cc50bea4cd1efd426f8` on `feat/quick-entry-current-session-voice`.

- Focused UI: 7 files / 65 tests passed.
- Focused Electron: 3 files / 40 tests passed.
- Desktop typecheck passed; lint passed with 0 errors and 88 pre-existing warnings; `git diff --check` passed.
- Specification closure review: PASS. Quality/security closure review: APPROVED.
- Windows build: `npm run build --workspace apps/desktop` exited 0; `assert-dist-built` verified renderer assets and staged `node-pty`.
- Real Windows canary: Quick Entry projected `Voice ready`, then the existing composer projected listening/transcribing/speaking. The finalized transcript `Hermes, use a tool to check the current date.` entered the already-selected canonical session; Hermes executed the visible tool receipt `date '+%A, %B %d, %Y'`, generated TTS audio, and returned to the inactive `Start voice conversation` control.
- Gateway PID remained `49272` before and after. The existing session was retitled `Casual Greeting and Mic Check`; no second session or gateway appeared.
- The spoken confirmation suffix was imperfectly transcribed and ambient speech produced additional turns while the microphone remained active. This is transport/STT behavior, not an authority or routing failure.
- No remote push, pull request, merge, live gateway replacement, or modification of an original checkout occurred.
