---
title: Hermes Native Realtime HUD — Architecture Source Assessment Receipt
status: incorporated-into-architecture-draft
date: 2026-08-11
dispatch: deleg_bc09331c
architecture: 05-companion-architecture.md
reconciliation: 06-intent-architecture-reconciliation.md
---

# Hermes Native Realtime HUD — Architecture Source Assessment Receipt

> **Posture:** This is a receipt for three parallel read-only source and
> traceability assessments used to correct the architecture draft. It is not the
> independent adversarial architecture/security/concurrency review, an
> architecture freeze, implementation authorization, activation authorization,
> or live-canary authorization.

## 1. Assessment lanes

### Hermes host reachability

Inspected exact local snapshots:

- canonical ingress: `d03d60fe3cafe9289513e53bacef0330e3916509`;
- native output controller: `faf4f4559f3e8605046aa8a5ed54406e646a2b35`;
- Discord generic-output sibling: `86c756de8c69a938db26ae5fd23829315cae8fa4`.

Full receipt:

```text
C:\Users\Degen\AppData\Local\hermes\cache\delegation\subagent-summary-0-20260811_101349_012850.txt
```

### Talk runtime and migration boundary

Inspected clean Talk `main` at:

```text
28d7068521b3e3e4d2e37a17220a5698455d86aa
```

It confirmed a production-reachable legacy provider-owned duplex runtime and a
separately registered API-v2 input-only core provider facade, but no `/talk core
join` or canonical attachment chain on `main`.

The separately inspected canonical attachment branch remains:

```text
99d8f2a3971db074bd2649c56d9afc91b63c57e2
```

Full receipt:

```text
C:\Users\Degen\AppData\Local\hermes\cache\delegation\subagent-summary-1-20260811_101349_013859.txt
```

### Frozen-intent traceability

Extracted 50 frozen invariants with owners, authority inputs, receipts, failure
semantics, and deterministic proofs; separately identified architecture-time
decisions, PRD-deferred decisions, and anti-goals.

Full receipt:

```text
C:\Users\Degen\AppData\Local\hermes\cache\delegation\subagent-summary-2-20260811_101349_014867.txt
```

## 2. Material source findings

1. The canonical attachment, exact-session admission, turn-slot linearization,
   and persisted finalization receipt are production-reachable when consumed by
   an external contextual plugin/provider.
2. The native output contracts and controller are implemented and well-tested but
   test-only in reachability: production attachment construction passes no output
   sink and no host finalization is projected into `project_host`.
3. No pre-TTS native reservation exists. Generic auto-TTS can run before the
   finalization receipt reaches the controller.
4. `86c756de` is a sibling generic-output behavior, not a dependency of the native
   controller. Its `MessageType.VOICE` change can activate generic TTS without
   native provider identity, lease, or drain proof.
5. No production Discord streaming PCM sink/lease exists.
6. No response-local barge-in coordinator invokes exact provider response
   cancellation plus exact lease interruption.
7. No retained, exactly-once replacement-turn barrier exists.
8. Current controller lifecycle can expose canonical `COMPLETED` before native
   output and never emits the stronger combined post-drain completion.
9. Talk `main` registers an input-only core provider but its production `/talk`
   command launches the legacy provider-owned dialogue/tool executor.
10. Talk's reusable assets are the low-level OpenAI wire/auth/session payload,
    defensive API-v2 boundary, transcript ledger, PCM conversion, receiver-tap
    knowledge, and redacted diagnostics—not the legacy prompt/tool executor or
    duplex playback takeover.
11. Exact Discord platform-principal authorization must not reuse coercive
    `int(...)` participant classification, display metadata, SSRC, provider
    labels, or voiceprints.
12. Architecture must decide explicit capture/mute authority, pinned-turn ordering,
    data retention, disclosure policy, config ownership, exact canary operations,
    and cross-repository evidence rather than leaving them implicit.

## 3. Corrections incorporated

The architecture and reconciliation were corrected to:

- replace three incorrect Agent SHAs with direct `git rev-parse` receipts;
- classify `86c756de` as a generic-output sibling and quarantine its trigger until
  atomic native reservation exists;
- state that output construction/projection/reservation remain production gaps;
- require exact positive native Discord principal IDs without coercion;
- add immediate generation-fenced host mute/unmute/detach semantics;
- define pinned correction ordering relative to typed turns;
- define Discord MVP `operator_only` disclosure with membership revalidation;
- put non-secret behavior in typed `config.yaml`/existing setup UX, not new
  `HERMES_*` environment variables;
- define memory-only microphone/transcript/PCM-spool retention and bounded receipt
  retention;
- add behavior-level capability-invariance evidence without exposing prompt text;
- fix the exact harmless canary tool to
  `skills_list(category="autonomous-ai-agents")` with `hermes-agent` expected;
- fix the bounded no-tool delegation receipt to
  `native-hud-delegate-ready`;
- preserve the distinction between this source assessment and the still-pending
  independent adversarial architecture review.

## 4. Result

**Result: `SOURCE_ASSESSMENT_INCORPORATED`**

The companion architecture remains `draft-for-adversarial-review`. The source
assessment corrected grounding and filled architecture decisions; it did not
freeze the architecture or authorize implementation.
