# Discord voice task context

`register_command(..., invocation_context=True)` opts a gateway plugin command into an
`invocation` keyword. Other surfaces keep the legacy call; handlers default invocation
to `None`. While the authenticated handler runs, call
`invocation.capture_discord_task_context_proof()` to receive `{proof, expires_at}` plus
the verified public `surface`, `guild_id`, `channel_id`, `operator_user_id`,
`audience_revision`, `audience_user_ids` and `profile`. These fields are assertions for
initial client binding; redemption and fresh checks remain mandatory before sharing.
Capture also returns `anchor_session_id` from the exact originating Discord routing
entry. If this is the first command, the normal `SessionStore.get_or_create_session`
path creates its profile-scoped conversation with `touch_activity=False`, only after
all speaker/listener checks pass. It starts no model work and preserves existing
history and routing. A trusted Talk proxy may keep the proof at issuer/profile/anchor
while its independent TargetCatalog authorizes another selected task/profile/peer.
Verify locally before each selected-peer request and before releasing returned data;
never send a local opaque proof to a remote issuer or treat it as remote authority.
The capability closes when the handler returns, including asynchronous handlers.
It accepts no operator, guild, channel, profile or audience arguments.

The host requires the exact registered Discord adapter that created the inbound
source, a ready connected client, a cached human guild member currently in voice,
and an explicit Discord member or guild-role allowlist grant for the operator and
every human listener. A bystander without a grant denies the entire room. Wildcards, allow-all,
text channel grants, dashboard shared operator status and relay identity fields do
not suffice. The complete voice-state audience must resolve to known guild members;
bots are excluded from the sorted `audience_user_ids`. Missing cache data fails closed.
Audience changes increment a room revision even while core voice is disconnected;
leave/rejoin and reconnect invalidate a previously issued proof.

The gateway and dashboard may be separate processes or configured remote peers. Use
the selected configured gateway bearer and the existing `/p/{profile}` prefix. Never
select a peer from proof contents or infer trust from loopback. A proof belongs to its
issuing process and profile; restart invalidates it. Send it privately between trusted
components, never as command reply text, model context, a URL query, logs or transcript.
Raw client `{surface, guild_id, channel_id, operator_user_id}` fields are selectors only;
compare them against verified output and reject a mismatch.

All context endpoints are POSTs with `Cache-Control: no-store`:

- `/v1/task-context/discord/redeem`: `{proof, session_id, binding_id}`. The selected
  canonical session must exist. First redemption pins the configured-key owner,
  profile, session and opaque Talk connection binding; exact retries are idempotent.
- `/v1/task-context/discord/verify`: the same body. Recheck before each room action and
  immediately before audible delivery. Returns `surface`, `guild_id`, `channel_id`,
  `operator_user_id`, `audience_revision`, sorted human `audience_user_ids`, `profile`,
  `session_id`, `binding_id`, `expires_at`. None of these are text-destination grants.
- `/v1/task-context/discord/rebind`: the same body plus `next_session_id` and
  `next_binding_id`. After current live validation, atomically retire the old binding
  and return the verified new context plus a new `proof`. Accepted jobs are unchanged.
- `/v1/task-context/discord/revoke`: the original triple. Retires voice access only;
  it also works after audience/lease expiry and does not cancel canonical work.

Unused proofs expire after 300 seconds. Redemption establishes a 15-minute sliding
lease, renewed only by successful live checks, with an eight-hour absolute lifetime
from the original event. Rebinding does not extend the absolute lifetime. A detected
membership/audience mismatch permanently retires the proof even if the member rejoins.
Expired or retired bindings need a new trusted command event, not a dashboard refresh.

A linked `/v1/runs` admission carries `discord_task_context` with the same proof/session/
binding triple. Validation precedes durable origin and child writes. Configured workers
retain their configured execution policy; the client cannot supply workspace, model,
policy or toolset overrides. The worker receives an immutable `room_context` assertion
without the proof; its `still_authorized()` callback remains canonical task/lease
ownership so closing or switching voice never kills accepted background work or drops
its durable result.

For voice reads, events, steering, stop and approvals, also send
`X-Hermes-Discord-Task-Proof`. The host checks it against the owned run's canonical
parent session on every action and before each streamed frame. Room approvals require
one exact request and permit only `once` or `deny`; they cannot grant session/permanent
approval. The Talk server must retain this header for every room-facing action and
recheck before speaking. Ordinary canonical task access keeps its existing bearer
ownership and continues after the voice binding retires; it does not assert Discord
identity or permit delivery to a room. Existing RoomLink grants remain separate and
cannot redeem Discord proofs or bypass their own execution restrictions.
