# Dashboard task context for plugins

Server-side dashboard handlers can call:

```python
from hermes_cli.dashboard_task_context import resolve_dashboard_task_context

context = resolve_dashboard_task_context(request, profile="alpha")
```

Call after the existing dashboard authentication middleware. The frozen `DashboardTaskContext`
contains `principal_id`, `principal_kind`, `profile_name`, server-only `profile_home` (Path), and
`store_id`. The helper reuses existing session-token verification, canonical profile resolution,
and the profile DB owner to read store identity. That owner may bootstrap an absent/empty DB.
The helper does not parse credentials, grant permissions, or create task/approval authority.

Middleware-verified OAuth/password/native Session objects produce `principal_kind="verified_session"`.
The ID is `dashboard-session-` plus SHA-256 of compact structured JSON `[provider, org_id, user_id]`.
Token rotation and display/email changes do not change it; provider/org/user changes do. Identity
fields must be valid strings, provider/user nonblank; empty org_id is valid. Authentication and
session freshness remain owned by the middleware, not a second verifier in this helper.

In legacy local-token mode, a valid current dashboard token produces the exact shared role ID
`shared-dashboard-operator` and `principal_kind="shared_dashboard_operator"`. It represents all
holders of that dashboard credential, **not an individual person**. Token rotation preserves role
continuity without retaining or hashing the token. Consumers scope this role by their configured
host and canonical profile; principal_id alone must never identify a global person or grant access.

Invalid/malformed sessions fail closed without falling back to the shared role. OAuth-required
mode without verified session denies. Machine TokenPrincipal/token_authenticated contexts are
unsupported. Loopback, auth.source, body actor fields and unverified client claims establish no
identity. Authentication failures raise HTTP 403; profile validation retains existing 400/404 errors.

Keep the entire context on the server. It contains no credentials and has no wire serializer;
profile_home is deliberately excluded from repr. Do not return the dataclass or asdict(context)
from an HTTP handler. A connection layer should expose only its own opaque browser connection
reference. Existing passive-route principal behavior is unchanged by this additive SDK helper.

## Comparable profile store identity

Authenticated `GET /v1/passive-history/capabilities` (also `/p/{profile}/v1/passive-history/capabilities`)
adds `store_id` to the existing passive capability object and returns `Cache-Control: no-store`.
It is derived from the actual selected profile DB, never a client-supplied profile label or path.
The strict route requires a configured valid API key; default no-key mode denies. The public
`GET /v1/capabilities` response never contains a store ID.

Compare this ID with the server-only context's `store_id` before attachment, writes or work.
A mismatch means the configured gateway does not refer to the same canonical store, even if
profile names and session IDs match. Identity proves neither permission nor session freshness;
existing authentication, attachment and lineage checks remain necessary.

`hermes_state_store_identity.get_store_id(db)` hashes the existing DB generation, application-ID
stamp and physical file identity. Independent handles to the same file agree; ordinary writes
preserve the ID. Copies and recovered stores differ, including recovery installed in place with
a fresh application stamp. No path or secret is included in the returned value, and no registry
or new persistent stamp is created. Unknown file identity or missing stamps fails closed (HTTP
503 at these consumption seams). Restoring a file in place while preserving **all** these existing
stamps and its physical identity is outside this lifecycle detector. This is a proof at read time.

## Fresh owning-run approvals

`GET /v1/runs/{run_id}/approval` (also under `/p/{profile}`) uses the existing approval auth and
owning-run scope. It requires configured API-key authentication or an applicable room grant;
unattended public/no-key mode cannot read this queue. The capability `run_approval_list` advertises
the route. Its response has `Cache-Control: no-store`:

```json
{"object":"hermes.run.approvals","run_id":"run-id","status":"waiting_for_approval","approvals":[]}
```

`approvals` contains the existing public pending-approval payloads from the live owning-run queue,
using the same projection as the gateway RPC reader. It excludes callbacks, wait events and
credentials. Stopping or terminal runs report no actionable approvals. Resolution or callback
revocation is visible on the next read; cached run metadata is not the source. Reads never
register, revive, decide or start work. Existing `POST` on the same subresource remains the decision
authority and rechecks stopping/terminal state immediately before resolving the queue.
