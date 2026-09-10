# Passive history ingress v1

Authenticated clients can attach to an existing conversation, read a bounded text snapshot,
and save finalized speech or other external dialogue. These operations never create an agent,
run tools, submit work, mint approvals, create a session, or reopen a closed session.

Discover support before sending anything. The gateway advertises `features.passive_history`
in `GET /v1/capabilities`; dashboard `/api/status` advertises `passive_history`. Both also
serve the authenticated `GET <prefix>/capabilities`. Require `version: 1` and `passive_only: true`.
An unsupported host must not be replaced with a call to chat merely to persist text.

Gateway prefix: `/v1/passive-history`, using the existing `Authorization: Bearer ...` key.
Named gateway profiles use `/p/{profile}/v1/passive-history` and that profile's key.
Dashboard prefix: `/api/passive-history`, using the existing dashboard session-token header
or verified OAuth identity. Dashboard operations accept the established `?profile=name` selector.
Profile authority and DB resolution remain with the host. Paths and credentials cannot appear in bodies.

| Method | Suffix | Required JSON fields |
|---|---|---|
| POST | `/attach` | `tab_id`, `session_id` |
| POST | `/snapshot` | `tab_id`, `attachment_id`, `generation`, `session_id` |
| POST | `/commit` | attachment fields plus `event_id`, `origin_turn_id`, `messages` |
| POST | `/reconcile` | `session_id`, `event_id` |
| POST | `/adopt` | `session_id`, `event_id`, `origin_turn_id`, singleton user `messages` |
| POST | `/detach` | attachment fields |

Unknown fields are rejected. Event, origin, tab and attachment IDs are 1–128 ASCII letters,
digits, dots, underscores or hyphens; use independently generated UUIDs for events/tabs.
Session IDs are exact host-issued strings, at most 256 characters. Generation is a positive integer.

Attach returns the identity fields, canonical profile and a snapshot with `conversation_id`,
current `session_id`, `messages`, `truncated`, and capabilities. Preserve the outer `session_id`
as the original selected target; a compression successor inside the snapshot is not a target switch.
Snapshots contain at most 20 recent user/assistant text rows and 32 KiB combined UTF-8 text.
They omit system prompts, tools, configuration, API sidecars and non-text payloads.
Compaction handoffs use the canonical session display projection: internal summaries are removed,
while genuine earlier text inside a merged carrier remains visible. Internal notification kinds
and empty text rows are omitted; multibyte truncation never fabricates empty messages.

Example commit, using identity fields returned by attach:

```json
{
  "tab_id": "tab-uuid", "attachment_id": "returned-id", "generation": 1,
  "session_id": "original-session-id", "event_id": "event-uuid",
  "origin_turn_id": "utterance-uuid",
  "messages": [{"role": "user", "content": "Finalized spoken text."}]
}
```

Messages must be one finalized `user` or `assistant` message, or an ordered `[user, assistant]`
pair. Only `role` and `content` are accepted. Text is nonempty and at most 64 KiB UTF-8 per
message; the complete HTTP body is capped at 160 KiB. Host-owned provenance is never permission.

`saved` and `already_saved` return a receipt with the original message IDs, conversation/segment
IDs and external-history revision. Receipt IDs are not execution or approval IDs. Equal retries
retain the stable event and origin IDs; changing payload or owner under an existing event conflicts.
Canonical history remains in original user/assistant roles and is visible on the next authorized turn.

Each principal/profile/tab has independent attachment authority. Reattaching replaces only that
tab's attachment. Commit validates the exact original target, principal, profile, attachment ID,
generation and host epoch inside the same SQLite writer transaction as the canonical insertion.
Detach revokes only the exact current generation; an old detach cannot revoke a newer attachment.
An active ordinary turn causes a retryable busy response; passive operations never hold its lease.

A host restart invalidates attachment authority. After a lost response, call read-only reconcile
with the original target and event: `saved` proves the existing receipt, `unknown` proves only that
no receipt was found, and `retired` means its canonical rows were removed. If unknown, reattach to
the original target before retrying a fresh commit. Never retarget a pending event to the newly
selected conversation. Receipt reconciliation works independently of attachments or host epochs.

Session deletion cascades attachment removal. Message deletion invalidates attachments referencing
the affected original, conversation-root or snapshot segment. Receipts retain content-free
tombstones; delete/recreate cannot revive them. Clients discard derived context when attachment
validation fails and must reattach. There is no lifecycle push stream in this version.

Errors have `{error, retryable}`: authentication 401/403; invalid message/body 400; body-size cap 413;
missing target 404; stale attachment, target unavailable, event conflict or busy 409; retired 410;
SQLite unavailable 503. Only busy/store-unavailable are marked retryable. Error bodies omit
transcript contents, paths and credentials. Closed or ambiguous lineages refuse without selecting a sibling.

Origin adoption is supported only for opt-in API runs: capabilities include `origin_adoption: true`
and `origin_adoption_sources: ["api_runs"]`. This is a read-only proof operation, never permission
to execute or an instruction to submit the utterance again.

For a new authoritative request, `POST /v1/runs` accepts an optional
`origin: {"event_id":"event-uuid","origin_turn_id":"utterance-uuid"}`. It requires a durable
`Idempotency-Key`, an explicit existing `session_id`, and plain-string `input` containing the
original user utterance. Origin requests cannot use caller-supplied history, response chains,
or hosted-room/child-worker dispatch. Ordinary requests without origin retain existing behavior.

Before scheduling the run, the host reserves the event in state.db. Passive insertion of that
event then conflicts; if passive persistence already won, run admission conflicts instead of
launching another canonical input. The run's user row and origin binding commit atomically.
Generated assistant/tool text and later independent requests do not receive that user-origin claim.

Call `/adopt` with the original target/event/origin and the single original user message. It
verifies canonical owner, payload, host-written provenance and committed row ID. `adopted`
returns the original run ID and message IDs; `pending` returns no message IDs. `reserved: false`
means no reservation was found, which grants no fresh-write or execution authority. Changed
payload/owner returns conflict; removed canonical references return retired. No history is appended.
The run ID is only a link: its existing authorization still governs access to run controls.

202 admission, running/completed status, and matching text alone are not canonical-row proof.
A lost-response retry uses the same Idempotency-Key. An uncertain/crashed pending reservation
remains pending and prevents a passive fallback; this version does not automatically release it
or restart an execution that might still finish. Do not retrofit legacy runs by matching text.
An explicit origin-admission refusal before dispatch is terminal for that Idempotency-Key:
the original HTTP error is stored and replayed with `retryable: false`, never converted into 202.
Deferred input flushes retain their own host-created origin claim after the executor exits.
Dashboard-origin execution and steering propagation remain unsupported separate work; the dashboard
can read authenticated adoption proofs for API-origin rows in its authorized profile.

Compatibility: schema 33 adds canonical execution-origin reservations/bindings and deletion
tombstones over schema 32's attachment storage. Database recovery deliberately drops attachment
authority while retaining passive receipts and execution origins with their canonical row IDs.

## Linked child work on `/v1/runs`

Hosts advertising `features.linked_child_dispatch.version: 1` accept a `child` object alongside
the original `input`, explicit parent `session_id`, `origin` and durable `Idempotency-Key`:

```json
{
  "session_id": "existing-parent", "input": "Exact original user utterance",
  "origin": {"event_id": "utterance-event", "origin_turn_id": "voice-turn"},
  "child": {"goal": "Derived execution goal", "context": "Bounded child context",
            "correlation_id": "stable-action-id", "allowed_toolsets": ["file"]}
}
```

Goal is nonempty, at most 16000 characters; context is optional and at most 32000 characters.
Correlation IDs use the existing bounded ASCII identifier rules. Optional allowed_toolsets is
a nonempty list of at most 32 known toolsets and remains subordinate to host permissions.
Model/role/cwd overrides, arbitrary child keys, room dispatch and caller history are unsupported.

Fresh original input commits through the existing passive writer. A prior matching passive
receipt reuses its exact user row, including a user/assistant pair's user row, without modifying
provenance. Supply `origin.receipt_id` for reuse-only intent: a missing/foreign/mismatched receipt
then refuses instead of inserting fresh input. Execution-origin reservations from parent-model
runs are not supported as child origins by this version. The original utterance is never replaced
with the generated child goal, and this mode never invokes the parent's model.

Distinct actions may reference one utterance; each needs its own Idempotency-Key and correlation.
Complete request fingerprints bind goal, context, tool restrictions and origin. Canonical origin/
dispatch links commit before launch, and a durable CAS consumes launch authority. Response loss,
process-local registry loss or restart never authorizes replacement execution for that dispatch.
Run status exposes parent_message_id and, once known, child_id/child_session_id. Lifecycle handles
now include optional child_session_id; their capability tokens remain inside the host.

The existing run's auth/status/events/approval/stop ownership stays active through child terminal
completion. Child context is copied across executor boundaries; stop cancels through the public
lifecycle service. Construction or permission failures never fall back to a parent-model run.
Schema 34 adds child_dispatches to the same canonical DB/recovery path with deletion tombstones.
Child IDs may remain unknown after an uncertain launch; that is not permission to launch again.
Rollback to a prior executable leaves additive tables harmless, but clients must treat missing
capabilities as unsupported. Local tests and commits do not establish deployed availability.
