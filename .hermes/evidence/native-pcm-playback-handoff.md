# Native PCM Playback Ownership — Closure Handoff

Date: 2026-08-12

## Decision

**GO for the next speech-start barge-in barrier or activation-planning slice only.**

This is **not** authorization to install, restart, activate, deploy, push, merge, join a Discord voice channel, or run a live/audible canary.

## Immutable inputs

- Agent pre-playback base: `78b830ff163ccb359d9245eef323d897058b1143`
- Talk provider dependency: `6c3ed3a24cb87b2b493ba5b7faca8d116f9d5bd1`
- Provider-fidelity evidence: `6210253ea1a2b991534d7495190c08af3b01b8dd`
- Final playback code/test head: `69a7d1d62d342d8bd0864beaa613679743c5212c`
- Branch: `feat/native-playback-ownership-20260812`
- Isolated checkout: `C:\Users\Degen\isolated-dev\hermes-agent-native-playback-20260812`

This evidence document is committed above the code head. Resolve the final docs-only branch tip with `git rev-parse HEAD`; verify its parent is `69a7d1d62d342d8bd0864beaa613679743c5212c` and that the tip changes only this evidence file.

## What is implemented

- Agent-owned, identity-protected Discord PCM playback lease.
- Exact PCM16LE 24 kHz mono to PCM16LE 48 kHz stereo conversion.
- Bounded queues, writes, identities, replay tracking, and task ownership.
- Capacity includes the frame returned to the sender but not yet acknowledged.
- Provider bytes are accounted incrementally and truthfully.
- Local drain is acknowledged only when the Discord sender advances to its next mixer read.
- Response-local interruption preserves unrelated ambient and ordinary speech.
- Adapter-owned silent mixer-bus installation without implicit ambient activation.
- Exact VoiceClient/mixer identity and generation fencing.
- Unknown or replaced mixers remain untrusted.
- Consume-once native-output reservation from the exact durable assistant row.
- Independent immutable issuance proof for canonical text digest and authority fields.
- Controller-native explicit response execution with `allow_tools=False`.
- Exact provider response, turn, and output-item binding.
- Ordered direct PCM writes without a second controller audio queue.
- Provider completion is distinct from local Discord sender-boundary drain.
- Strong `COMPLETED` occurs only after validated normal drain and host retirement.
- Missing/empty audio, odd PCM, forged events/receipts, stale generations, and native failures fail closed with no generic TTS fallback.
- Exact response cancellation and lease interruption share one terminal owner.
- Close waits truthfully for acquisition and lease cleanup before `CLOSED`.
- Provider cleanup errors and cancellation are retryable; no premature `CLOSED`.
- Provider transcript projection is bounded and does not retain provider speech text.

## Authority and provenance

Hermes Agent remains the only reasoning, tool, approval, memory, persistence, and session authority. Talk/OpenAI Realtime is transport plus duplex audio rendering only. Provider tools are disabled/inert. Provider transcript text and metadata cannot replace the canonical saved assistant answer.

Generic TTS cannot silently claim or replace a native-owned response after reservation. Native failure remains a native failure.

## Honest completion boundary

`native playback drained` means:

1. the provider emitted the exact response completion event;
2. the final lease frame was returned by the mixer; and
3. discord.py's sender thread advanced to the next `VoiceMixer.read()`, proving the prior synchronous `send_audio_packet()` call returned.

It does **not** prove remote Discord receipt, device playback, human audibility, or that a listener heard the response.

## Verification receipts

### Final affected Agent gates at code head `69a7d1d...`

- Controller + messaging host/integration + TUI host/integration: `160 passed`.
- Ruff on the final changed controller/test files: passed.
- Python compilation on the final changed controller/test files: passed.
- `git diff --check`: passed.
- Worktree: clean.

### Installed-path and playback gates

- Focused playback plan suite: `227 passed, 1 deselected`.
- The deselected test was `TestVoiceReception::test_on_packet_dave_unencrypted_error_passthrough`; its optional dependency was unavailable: `No module named 'davey.davey'`.
- Final reviewer broader realtime-voice aggregate before the last narrow fixes: `146 passed`.
- Final cumulative closure spot checks after the narrow fixes are included in the final `160 passed` receipt.

### Talk compatibility

At exact Talk SHA `6c3ed3a24cb87b2b493ba5b7faca8d116f9d5bd1`, with the Agent candidate prepended to `PYTHONPATH`:

- Focused Talk contract/core gate: `204 passed`.
- Complete Talk suite: `987 passed, 1 skipped`.
- Talk worktree clean before and after.

### Broad Agent suite limitation

A complete Agent suite was attempted but stopped after more than two hours at approximately 31%. It had accumulated many failures outside this playback slice and produced no usable final failure summary. It is **not** reported as green. Closure relies on the affected Agent suites, installed-path integration proof, complete Talk suite, static checks, clean-state checks, and repeated independent adversarial reviews.

## Review closure

Independent reviews repeatedly exercised authority, identity, concurrency, cancellation, cleanup, bounds, privacy, and provider-vs-drain truth. Concrete findings were fixed under focused regressions. The last bounded defects closed at the code head were:

- exact provider output-item binding;
- rejecting missing/zero audio completion;
- bounded non-retaining output transcripts;
- truthful retryable provider cleanup on normal failure and cancellation.

No unresolved critical or important playback finding remains in the bounded closure scope.

## Explicit exclusions and next barrier

Not implemented or authorized here:

- speech-start barge-in admission;
- replacement-turn buffering/admission;
- installation or profile activation;
- service or gateway restart;
- live Discord join or audible canary;
- remote-delivery/audibility claims;
- push, PR, merge, or deployment.

The next slice must plan and prove the speech-start barge-in barrier (or separately plan activation) and obtain another explicit GO before any installation, restart, activation, or live canary.
