# Native Realtime HUD — Slice 1 Implementation Handoff

**Status:** slice-1-complete-final-integration-approved
**Date:** 2026-08-11
**Publication:** local only; no push, PR, merge, install, restart, activation, or live canary

## 1. Resume locations

### Hermes Agent candidate

```text
Path:      C:\Users\Degen\isolated-dev\hermes-agent-native-hud-slice1-20260811
Branch:    feat/native-hud-slice1-ingress-fence
Base:      d03d60fe3cafe9289513e53bacef0330e3916509
Code head: 08b62a2ff88bc501d58fc983379562d765494ecc
```

The branch tip is a clean docs-only descendant containing this handoff. Resolve
its exact immutable SHA with `git rev-parse HEAD`; verify its parent is the code
head above.

### Hermes Talk candidate/reference

```text
Path:   C:\Users\Degen\isolated-dev\hermes-talk-native-hud-slice1-20260811
Branch: feat/native-hud-slice1-talk-proof
Head:   99d8f2a3971db074bd2649c56d9afc91b63c57e2
State:  byte-identical and clean; no Talk production or test changes
```

## 2. Frozen governing artifacts

```text
Architecture:
C:\Users\Degen\isolated-dev\hermes-agent-native-hud-intent-20260811\docs\plans\voice-hud-agent-parity\05-companion-architecture.md
SHA-256: 0ed3360a5cde187f6aea13e51a24ca893f755d0a8098fd9af5be7df248fec3b5

Implementation plan:
C:\Users\Degen\isolated-dev\hermes-agent-native-hud-intent-20260811\.hermes\plans\2026-08-11_144320-native-realtime-hud-slice-1.md
SHA-256: dc091320f7c62e8773dff61e4514b140ec212cfc4606265467226ed95feed68a
```

PR #65845 remains reference-only. This slice imports no AG-UI package or runtime.

## 3. Local implementation commits

```text
58c345002ca2901ad856b3ad711b79e77c26600c
fix: fence native realtime turns from streaming tts

08b62a2ff88bc501d58fc983379562d765494ecc
fix: reserve native turns ahead of generic auto tts
```

Combined binary diff SHA-256 from frozen base through implementation head:

```text
68962292cc6f9535a83c00ecb904dd11bf103469dabba0cc87339437936c9ac3
```

Changed production/test files:

```text
gateway/platforms/base.py
gateway/realtime_voice_messaging_host.py
gateway/run.py
tests/gateway/test_realtime_voice_messaging_integration.py
tests/gateway/test_streaming_tts_gateway_regression.py
```

## 4. Implemented behavior

### 4.1 Streaming-TTS admission fence

The canonical host already validates the opaque attached-turn claim and passes
`force_nonstream=True` into the ordinary Hermes agent run. The implementation
adds `not force_nonstream` at the existing `StreamingTTSConsumer` construction
gate.

Result:

- exact native-attached `VOICE` turn constructs no streaming-TTS consumer;
- ordinary unattached `VOICE` with `force_nonstream=False` remains eligible;
- no new parameters, capabilities, registries, tokens, or surface abstractions.

Strict TDD receipt:

```text
RED: native assert_not_called failed because the consumer factory was called once
Negative control before production edit: 1 passed
Exact GREEN: 2 passed
Regression file: 3 passed
Required three-file gate: 33 passed
```

Independent reviews:

```text
Spec:    PASS — no gaps or overbuild
Quality: APPROVED — no critical or important issues
```

### 4.2 Whole-file auto-TTS exact-claim fence

The implementation adds `_is_native_realtime_event(runner, event)`, delegating
directly to the existing `_claim_for` authority check. Whole-file auto-TTS checks
exact native ownership before the synthesis `try/except` and excludes only a
validated native-attached event.

Result:

- forged, stale, or mismatched claims raise fail-closed and cannot silently fall
  through to generic synthesis;
- the integration regression forces only the exact claimed event to `VOICE`;
- generic synthesis and audio tripwires remain untouched for ordinary events;
- canonical host classification remains `MessageType.TEXT`;
- sibling commit `86c756de` was not adopted or cherry-picked.

Strict TDD receipt:

```text
RED: AssertionError — native realtime turn reached generic audio output
Exact GREEN: 1 passed
Required focused suites: 37 passed
Ordinary unattached VOICE auto-TTS control: 1 passed
```

Independent reviews:

```text
Spec:    PASS — no gaps or overbuild
Quality: APPROVED — no critical, important, or minor findings
```

## 5. Baseline and final verification

### Before edits

```text
Agent focused baseline:                 108 passed
Installed Talk→exact Agent baseline:    157 passed; installed integration did not skip
```

### Final implementation head

Agent command scope:

```text
tests/gateway/test_realtime_voice_invocation.py
tests/gateway/test_realtime_voice_controller.py
tests/gateway/test_realtime_voice_messaging_host.py
tests/gateway/test_realtime_voice_messaging_integration.py
tests/gateway/test_streaming_tts_gateway_regression.py
tests/gateway/test_streaming_tts_consumer.py
tests/gateway/test_base_auto_tts_output_format.py
```

Result:

```text
110 passed
Ruff on all changed files: all checks passed
git diff --check base..HEAD: passed
```

Installed Talk command used the exact candidate through both
`HERMES_AGENT_REPO` and `PYTHONPATH`.

```text
157 passed
Talk checkout clean and byte-identical at 99d8f2a3971db074bd2649c56d9afc91b63c57e2
```

## 6. Known baseline debt

The Agent aggregate suite still emits two pending
`StreamingTTSConsumer._run()` teardown warnings. They reproduced on unchanged
consumer suites before this implementation. The new regression file itself does
not introduce a pending-task warning. Both independent reviews treated this as
pre-existing and outside the two admission-fence commits.

`uv` may also report the repository's pre-existing
`exclude-newer = "14 days"` TOML warning. It did not block tests or Ruff.

## 7. Explicitly not implemented or authorized

This slice does not claim or include:

- OpenAI Realtime canonical-text rendering;
- provider fidelity qualification;
- UTF-8 spool or PCM conversion;
- Discord native PCM playback;
- playback leases or physical drain receipts;
- response-local barge-in or pinned replacement;
- HUD presentation polish;
- AG-UI/Desktop generalization;
- installation, gateway restart, production activation, or live canary;
- push, PR, merge, or publication.

## 8. Exact next engineering step

Begin the provider-fidelity qualification gate before implementing the output
stack:

1. prove exact canonical text can be rendered to provider audio without semantic
   mutation, provider-added speech, tools, or alternate reasoning;
2. measure whether the provider exposes enough response/item identity for exact
   response-local interruption and playback correlation;
3. produce a deterministic pass/fail receipt;
4. only on PASS, plan the bounded UTF-8 spool → Realtime render → PCM sink slice;
5. on FAIL, stop and evaluate a different exact-text renderer without changing
   canonical Hermes authority.

Do not begin Discord playback, completion-after-drain, or barge-in until the
fidelity gate passes.

## 9. Resume checklist

At the start of a fresh/compressed session:

1. read this handoff;
2. verify Agent `HEAD` and clean status;
3. verify Talk `HEAD` and clean status;
4. rerun the focused Agent and installed Talk commands if any byte changed;
5. load the frozen architecture and implementation plan;
6. confirm no install/restart/canary authorization;
7. create a new short provider-fidelity qualification plan rather than widening
   Slice 1.

## 10. Final integration review

Final read-only review verdict: `APPROVED`.

The reviewer found:

- no authority gap between the streaming and whole-file TTS fences;
- exact five-file Agent scope and no Talk/runtime/output-stack changes;
- matching commit ancestry, artifact hashes, changed-file inventory, and binary
  diff hash;
- accurate ordinary VOICE/TEXT controls and baseline-warning attribution;
- provider-fidelity qualification is the correct next gate;
- this handoff is sufficient for `/compress` or fresh-session continuation.

Critical issues: none. Important issues: none.
