# Steering an existing API run

Use the owning run's existing `/v1/runs/{run_id}/steer` subresource. Profile-prefixed
`/p/{profile}/v1/runs/{run_id}/steer` works identically. Steering queues a correction to the
same live job. It does not stop, replace, restart, approve tools, or control audio playback.

`GET /v1/capabilities` advertises `features.run_steering` version 1 and `endpoints.run_steering`.
The receipt protocol requires a configured valid API key and a durable run admitted with an
`Idempotency-Key`. Existing run/profile ownership remains authoritative. Room approval grants
do not grant steering authority. Older hosts and unsupported runtimes require an explicit UI limit.

First read `GET /v1/runs/{run_id}/steer`:

```json
{"object":"hermes.run.steering","version":1,"run_id":"run-id","status":"running","supported":true,"reason":null,"kind":"linked_child","session_id":"child-session","turn_id":"host-issued:turn","child_id":"child-id"}
```

The target comes from the actual live child through the public lifecycle service, or the ordinary
in-process agent. A linked run never steers its materialized inactive parent, even before its child
handle is available. `supported:false` includes a reason; target fields may be absent then.
Do not infer support from a process, profile name, or historical run metadata.

Submit an immutable action with the exact target returned by that read:

```json
{"input":"Keep the revised budget constraint","control":{"version":1,"action_id":"correction-123","expected_session_id":"child-session","expected_turn_id":"host-issued:turn"}}
```

The optional `control.origin` is `{event_id, origin_turn_id, receipt_id}` for the exact passive
receipt-owned user row in the linked child's parent conversation. Input must match that row
exactly. Changed, deleted, foreign or unavailable origins refuse. The correction and unrelated
concurrent typed input retain separate parent origins; steering adds no parent row or model call.
The child receives the existing steer marker through its own execution history.

Ordinary runs support unbound corrections through their stock queue and canonical persistence.
**Already-persisted parent-origin adoption is unsupported for ordinary runs:** the stock queue
would otherwise create another canonical parent row. A client must not drop an origin reference
and retry as unbound to bypass this limit. This capability is not full origin parity for every lane.
Native app-server, ACP/detached and unrecognized backends are unsupported by this receipt protocol.
The legacy text-only POST remains available with its older, weaker acknowledgement semantics.

Each `(owner scope, run_id, action_id)` is durable. The fingerprint covers the complete input/control
body, including target and origin. Repeating the same POST returns the same stored receipt without
queueing again; changed content conflicts (409). Reconcile a lost response with
`GET /v1/runs/{run_id}/steer?action_id=correction-123`, which never sends or revives the correction.
Both reads and receipt responses use `Cache-Control: no-store`.

Receipts contain `object:"hermes.run.steer"`, `version`, `run_id`, `action_id`, `session_id`, `turn_id`,
`created_at`, `status`, and `evidence`; verified origins also carry `origin` and `parent_message_id`.
They store identifiers/evidence, never correction text or credentials. States mean:

- `queued`: the actual backend accepted text into its queue (`backend_queue_ack`). It may still
  finish or stop before consuming it. This does not prove model delivery or application.
- `rejected`: the run/turn/origin no longer accepts that action, or the backend refused it.
- `unsupported`: this backend or origin mode cannot honor the requested contract.
- `unknown`: durable reservation exists but queue acceptance was not durably established. A crash
  or lost outcome at that boundary must not trigger another submission under a fresh action ID.

No automatic `delivered` or `applied` transition is inferred from model output or run completion.
Replaying `queued` after completion reports historical queue admission; consult current run status
separately. Receipts follow the existing owning-run retention window and cannot outlive ownership.
An unknown outcome survives restart and never starts replacement work.

The public `SubagentLifecycleService.steering(handle)` and `.steer(handle, text,
expected_session_id=..., expected_turn_id=...)` verify existing handle capability/parent ownership
and live child state. They reuse the stock steer queue. Cancelling or ending the child revokes
further steering. Stop-speaking, interrupting a turn, cancelling work and steering are distinct actions.
