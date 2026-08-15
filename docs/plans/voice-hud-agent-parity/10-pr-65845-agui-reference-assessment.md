---
title: Hermes Agent PR #65845 — AG-UI Reference Assessment for Talk-First Native HUD
status: incorporated-into-architecture-draft
date: 2026-08-11
pr: https://github.com/NousResearch/hermes-agent/pull/65845
review_mode: read-only isolated checkout
---

# PR #65845 Reference Assessment

## 1. Exact PR receipt

| Field | Value |
|---|---|
| Title | `feat(agui): Hermes AG-UI adapter` |
| Author | Markus Ecker (`mme`) |
| State | open, non-draft |
| Mergeability | GitHub reports mergeable, merge state `blocked` |
| Base | `main` at `c0106e50e7ecedb3ce34e785d949725dc4e0e457` |
| Head | `mme/hermes-ag-ui-support` at `b036d8be6d9786a7117777c8c3c2b40a84d2ca3b` |
| Scope | 33 changed files, +5,968 / -8 |
| CI | no checks reported on the head branch |
| Local checkout | `C:\Users\Degen\isolated-dev\hermes-agent-pr-65845-agui-20260811` |
| Local branch | `inspect/pr-65845-agui` |
| Local status after inspection | clean |

The PR adds a standalone `agui_adapter/` package, CLI/setup/config integration,
security documentation, optional dependencies, and an extensive test tree. It
does not add a canonical Hermes session-attachment API.

## 2. Current merge blocker reproduced

At the exact head:

```text
uv lock --check
-> lockfile needs to be updated

uv export --frozen --extra agui --no-emit-project --output-file /dev/null
-> Extra `agui` is not defined in the project's optional-dependencies table
```

`pyproject.toml` declares the AG-UI extra, but the exact-head `uv.lock` contains
no `ag-ui-protocol` occurrence and has no three-dot diff from the exact base.
This reproduces the current review blocker. No repository file was modified by
the checks.

The PR discussion reports local test runs from contributors, but GitHub shows no
CI checks for the exact head. Those reported runs are useful context, not an
independent exact-head CI receipt.

## 3. What the implementation actually does

### Run construction

`agui_adapter/session.py:436-503` constructs a fresh `AIAgent` for each AG-UI
run, resolves its model/provider/toolsets, and merges client-declared frontend or
state-writer tools.

`agui_adapter/server.py:143-243` translates the request, attaches callback
bridges, installs an approval callback on the worker thread, sets a session key
from AG-UI `thread_id`, and calls:

```text
agent.run_conversation(
    prep.user_message,
    conversation_history=prep.conversation_history,
)
```

### History and resume

`agui_adapter/translate.py:117-215` accepts AG-UI client messages as Hermes/OpenAI
history. On frontend-tool resume it reuses the prior user text and retains the
client-provided assistant/tool tail.

`agui_adapter/resume_shim.py` process-globally wraps Hermes
`build_turn_context`. A context variable enables removal of the synthetic user
turn only for AG-UI resume requests. The file itself identifies a gated core
`continue_from_history` hook as the cleaner alternative.

### Events

`agui_adapter/events.py:86-340` translates `AIAgent` text, reasoning, tool-start,
and step callbacks into AG-UI events. One bridge instance owns every open event
lifecycle for a run. Error cleanup closes text, reasoning, and dangling tool
starts before terminal `RUN_ERROR`.

### Approval park/resume

`agui_adapter/approvals.py` parks a blocked worker in a process-local registry
keyed by AG-UI `thread_id`. Resume pops the exact entry and resolves a future.
Unknown, malformed, cancelled, late, or timed-out decisions fail closed to deny.
Identity-scoped discard prevents an old timeout from deleting a newer entry.

### Disconnect

`agui_adapter/server.py:299-344` tracks one current `AIAgent` per `thread_id` and
calls `agent.interrupt()` when the SSE client disconnects. Compare-and-swap
unregistration prevents an older worker from evicting a newer slot.

### Security/configuration

The adapter uses config for behavior and environment only for the session-token
secret, fails closed for non-loopback binds without a usable token, guards Host
and JSON request posture, and rejects client tool names that collide with server
tools.

## 4. Reusable reference patterns

These patterns are valuable for Hermes Talk and later surfaces:

1. one stateful event bridge owns all open lifecycle spans for one run;
2. close all open spans before one terminal failure event;
3. fail closed for malformed, stale, unknown, cancelled, or timed-out resume;
4. identity-scoped compare-and-swap cleanup across timeout/resume races;
5. exactly one terminal success, interrupt, or failure projection;
6. typed behavioral configuration, with secrets only in secret storage/env;
7. reject namespace collisions before any dynamic client capability becomes
   callable;
8. test FIFO correlation, duplicate same-thread rejection, late resume,
   disconnect, dangling lifecycle cleanup, and race ownership explicitly.

These are references, not code-copy instructions.

## 5. What Hermes Talk must not copy

Talk must not adopt:

- AG-UI HTTP/SSE or CopilotKit as its transport dependency;
- client-supplied conversation history as canonical truth;
- a fresh `AIAgent` per voice turn;
- process-global frontend/state-writer tool registration;
- eager copied Hermes tool catalogs;
- the adapter-side `build_turn_context` monkeypatch;
- an AG-UI `thread_id` as authority for a durable Hermes session;
- transport disconnect as authority to cancel canonical/background Hermes work;
- the AG-UI parked-worker approval registry as a replacement for Hermes's
  existing approval records and surfaces.

Those choices serve AG-UI compatibility but do not prove the frozen Native HUD
requirement: one already-running durable Hermes session remains sole reasoning,
tool, approval, memory, persistence, and task authority.

## 6. Minimum universal seam, proven first by Talk

Do not extract an AG-UI-shaped framework before implementation. The smallest
surface-neutral Hermes contract has five capabilities:

1. consume-once attachment to an authenticated principal, exact route, and exact
   durable session;
2. canonical user-turn admission through the existing serializer;
3. opaque canonical-finalization receipt after durable persistence;
4. typed, explicitly scoped transport interruption that does not imply
   canonical/background-work cancellation;
5. typed lifecycle/terminal observation plus transport close.

Talk is the first implementation consumer. AG-UI or Desktop may consume this
same host seam later only after replacing client-history/fresh-agent ownership
with exact canonical session attachment.

This keeps the seam universal without building a general surface framework.

## 7. Architecture impact

The companion architecture was revised to:

- cite exact PR base/head and current blocked state;
- add the five-capability Talk-first universal seam;
- preserve PR event/race patterns as references;
- explicitly reject AG-UI history/fresh-agent/resume machinery for Talk;
- add admission-time fencing before generic streaming TTS;
- retain finalization-time native delivery reservation;
- make pre-lease interruption receipts conditional on whether a lease opened;
- reject every third Discord principal, including bots;
- add an early provider-fidelity qualification gate;
- remove speculative cross-surface and transcript-mirroring requirements.

The frozen product intent did not change. Implementation remains unauthorized
until the corrected architecture receives the final post-PR delta review and
freeze gate.
