# Voice HUD Agent Parity — Epic Index

**Program state:** Planning
**Readiness:** PRP-001 draft; later PRPs intentionally not implementation-ready

## North star

Quick Entry becomes the compact, always-available voice surface for the same active Hermes session. Turn-based full-capability parity ships first; provider-native realtime replaces the transport behind the same authority and HUD contracts later.

## Ownership

| Surface | Owns | Does not own |
|---|---|---|
| Hermes core/gateway | active session, turn lease, agent/tool policy, approvals, persistence, normalized realtime contract/events | provider credentials, Desktop presentation |
| Hermes Desktop | HUD, native mic permission, capture/playback, captions and projections, active target selection | tools, approval truth, provider credentials |
| Hermes Talk plugin | OpenAI Realtime adapter, terminal/Discord audio, immutable Discord speaker attribution, setup/doctor | general Hermes tool executor, canonical session persistence |

## Epic slices

| ID | Slice | Repository | Depends on | Exit receipt |
|---|---|---|---|---|
| PRP-001 | Quick Entry current-session voice launch | Hermes Agent Desktop | existing turn-based Desktop voice | HUD mic starts existing active-session voice; no new gateway/session |
| PRP-002 | Provider-neutral final-transcript admission | Hermes Agent core | current realtime provider/orchestrator contract | host permit + dedupe + bounded same-session ingress tests |
| PRP-003 | Gateway realtime session controller | Hermes Agent gateway | PRP-002 | typed open/feed/interrupt/close and normalized events scoped by profile/runtime/durable IDs |
| PRP-004 | Talk OpenAI adapter API-v2 migration | Hermes Talk | PRP-003 or stable core API | Talk registers a real core provider; stable turn/batch/call mapping |
| PRP-005 | Desktop realtime transport + HUD state | Hermes Agent Desktop | PRP-003, usable provider | HUD consumes backend audio/events; no provider secret in renderer |
| PRP-006 | Discord/terminal core-session adapters | Hermes Talk | PRP-002–004 | existing Talk audio/identity feed canonical core session authority |
| PRP-007 | Canonical work/approval projections | Hermes Agent + Talk adapter | PRP-003–006 | tools, approvals, subagents, Kanban and receipts reuse canonical state |
| PRP-008 | Provenance and memory convergence | Hermes Agent + Talk | PRP-006 | operator/participant provenance persists; participant text cannot become operator memory |
| PRP-009 | Resilience and presence truth | Hermes Agent + Talk | prior runtime slices | inactivity/reconnect/profile switch/teardown are visible, bounded and leak-free |
| PRP-010 | Setup, compatibility, docs and release gate | both | all | provider catalog/config/doctor, turn-based fallback, E2E fake-provider proof |

## Requirement ownership

| Requirements | Owning PRP |
|---|---|
| VH-007, VH-010, VH-017–021 for the turn-based HUD/current-session path | PRP-001 |
| VH-001–011 for provider-native realtime admission | PRP-002 |
| VH-012–016, gateway portion of VH-017/021 | PRP-003 |
| VH-023–025 provider registration/mapping | PRP-004 |
| VH-018–022 realtime HUD/audio | PRP-005 |
| VH-011, VH-024 Talk surfaces | PRP-006 |
| VH-006, VH-013, VH-022 approvals/work projection | PRP-007 |
| VH-007–011 provenance/memory | PRP-008 |
| VH-014–017 complete resilience | PRP-009 |
| VH-021, VH-025 compatibility/setup/release | PRP-010 |

## Critical path

`PRP-001` can ship independently and provides immediate full text-Hermes capability through voice, using the existing STT → canonical agent → TTS path.

Native duplex critical path:

`PRP-002 → PRP-003 → PRP-004 → PRP-005 → PRP-007 → PRP-009 → PRP-010`

Talk Discord/terminal convergence can proceed beside Desktop after PRP-004:

`PRP-004 → PRP-006 → PRP-008`

## Program gates

- Each PRP uses a fresh isolated worktree/branch.
- RED/GREEN evidence is required for behavior changes.
- One adversarial planning review per PRP maximum before implementation; later review moves to executable evidence and the final diff.
- Provider registration alone does not enable UI; capability checks require a usable provider.
- No PR may claim full parity before the canonical executor/session path, approvals, persistence, and release E2E agree.
- Push/PR requires explicit approval; merge is separately authorized.

## Backout

- PRP-001 can revert to text-only Quick Entry without data migration.
- Existing turn-based Desktop voice remains the compatibility fallback until PRP-010.
- Talk legacy mode remains capability-gated until the core-backed path proves parity; no silent downgrade.
- Closing/reverting voice surfaces must not alter persisted Hermes conversations or cancel background agents.
