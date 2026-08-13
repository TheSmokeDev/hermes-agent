# Response-Local Speech-Start Barge-In — Closure Handoff

Date: 2026-08-13

## Decision

**GO for controlled staging and a live Discord voice canary, subject to explicit operator authorization before modifying or restarting the installed runtime.**

This is not authorization to push, open a PR, merge, deploy broadly, or claim remote Discord receipt or human audibility without a live canary.

## Immutable inputs

- Agent playback-approved ancestor: `69a7d1d62d342d8bd0864beaa613679743c5212c`
- Final Agent barge-in code/test head: `943b04524ddca6200bbb77ce314a2db2df212d0a`
- Agent branch: `feat/native-playback-ownership-20260812`
- Agent isolated checkout: `C:\Users\Degen\isolated-dev\hermes-agent-native-playback-20260812`
- Exact Talk prerequisite: `6c3ed3a24cb87b2b493ba5b7faca8d116f9d5bd1`
- Talk isolated checkout: `C:\Users\Degen\isolated-dev\hermes-talk-native-preplayback-closure-20260812`

This evidence document is committed above the immutable code/test head. Resolve the docs-only branch tip with `git rev-parse HEAD`; verify its parent is `943b04524ddca6200bbb77ce314a2db2df212d0a` and its diff changes only this evidence file.

## Implemented contract

When exact provider speech-start arrives during one native response:

1. The controller synchronously fences that exact response and installs at most one response-local barrier.
2. The provider event pump remains live; it never waits on work needed to satisfy the barrier.
3. The controller cancels only the exact provider response ID.
4. The controller interrupts only the exact Discord playback lease and validates its physical-stop receipt.
5. The canonical host interruption gate must complete.
6. At most the first exact, bounded, final operator transcript matching the speech-start item is retained.
7. Replacement admission occurs only after exact provider terminal, validated playback interruption, and canonical host interruption converge.
8. The retained transcript re-enters the existing `FinalTranscriptAdmission` and native response-start path in the same canonical Hermes session.

Canonical Hermes remains the sole reasoning, tool, approval, memory, persistence, and session authority. OpenAI Realtime/Talk remains duplex audio and transcription transport. Provider tools are disabled/inert. There is no generic TTS or attachment fallback in this lane.

## Race and failure closure

Executable coverage closes:

- provider completion before speech-start while local playback still drains;
- speech-start before a legitimate late `ResponseStarted`;
- provider send acceptance distinct from definitive response start/end;
- blocked playback acquisition, PCM write, and physical drain;
- exact completion/cancellation terminal races;
- duplicate speech-start and duplicate terminal events;
- wrong response, turn, item, event subclass, and stale generation;
- partial/final ordering, conflicting finals, wrong item, participant provenance, character/UTF-8 bounds, and malformed surrogate text;
- public interrupt, controller close, `SessionFailure`, `SessionClosed`, and reconnect;
- provider-start and playback-acquisition failure;
- retained event-pump retirement before resume;
- failed host interruption ownership during concurrent public interrupt;
- self-await prevention, single terminal ownership, and retained-task cleanup.

Failures fence replacement admission and converge through sanitized lifecycle receipts. No raw provider error is promoted as durable authority.

## Verification receipts

At exact final Agent code/test head `943b04524ddca6200bbb77ce314a2db2df212d0a`:

- Fresh controller + provider gate: `220 passed`.
- Fresh messaging host/integration/TUI gate: `62 passed`, with 3 pre-existing Python 3.16 deprecation warnings.
- Final controller suite in closure review: `90 passed`.
- Adjacent Agent provider/host/integration closure-review suites: `192 passed`.
- Exact Talk contract/OpenAI compatibility with candidate Agent on `PYTHONPATH`: `26 passed`.
- Ruff on changed files: passed.
- Python compilation on changed files: passed.
- `git diff --check`: passed.
- Agent and Talk worktrees: clean.

The predecessor playback handoff separately records its broader installed-path and complete Talk-suite evidence, including `987 passed, 1 skipped` at the same Talk prerequisite SHA.

## Adversarial review closure

Five independent adversarial attack surfaces were used across implementation and repair: authority/identity/replay, concurrency/cancellation, exact specification/architecture, final specification/authority closure, and final concurrency/security/task hygiene.

Concrete findings were reproduced and fixed under regressions, including:

- failed barrier close self-await;
- forgotten completion-before-speech-start terminal state;
- malformed surrogate event-pump failure;
- timeout-bound pending-start termination;
- pending-start resumable-failure event-pump deadlock;
- premature retirement before legitimate late `ResponseStarted`;
- concurrent public interrupt dropping a failed host gate.

The final two closure re-reviews at `943b04524ddca6200bbb77ce314a2db2df212d0a` returned **PASS** with zero critical, important, or minor findings. The public-interrupt race was repeated 25/25 successfully in the prior final review cycle, and final focused closure matrices passed.

## Controlled staging and live-canary procedure

Only after explicit operator authorization:

1. Record the currently installed Agent/Talk versions, profile, service state, and rollback targets.
2. Stage exactly the pinned Agent and Talk SHAs above—never an unpinned branch tip.
3. Run installed-path discovery and compatibility checks before restart.
4. Restart only the required Hermes/Talk processes.
5. Confirm the same canonical Discord session, user authority, dynamic tools, approvals, memory, and persistence are active.
6. Join the intended Discord voice channel and run a bounded canary:
   - normal native response;
   - a harmless tool/approval turn through the same session;
   - interrupt during playback;
   - rapid repeated interruption;
   - interruption during delayed response start;
   - verify exactly one replacement turn and no duplicate/stuck audio;
   - verify no generic TTS, attachment fallback, second agent, or lost canonical session.
7. Capture provider terminal, exact lease interruption, physical-stop, host-gate, replacement-admission, and session-continuity receipts.
8. On any invariant failure, stop the canary and roll back to the recorded installed versions.

## Honest boundary

Local tests and reviews prove controller and adapter behavior, not remote delivery or human audibility. No installation, restart, Discord join, live cancellation proof, remote receipt, audible canary, push, PR, merge, or deployment has occurred at this handoff.
