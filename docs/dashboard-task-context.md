# Dashboard task context for plugins

Server-side dashboard handlers can call:

```python
from hermes_cli.dashboard_task_context import resolve_dashboard_task_context

context = resolve_dashboard_task_context(request, profile="alpha")
```

Call after the existing dashboard authentication middleware. The frozen `DashboardTaskContext`
contains `principal_id`, `principal_kind`, `profile_name`, and server-only `profile_home` (Path).
The helper reuses existing session-token verification and canonical profile resolution. It does
not open a DB, parse credentials, grant permissions, or create task/approval authority.

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
reference. Existing passive-route identity behavior is unchanged by this additive SDK helper.
