# Installed task worker providers

`PluginContext.register_task_worker_provider(provider)` registers a `TaskWorkerProvider`
in the existing profile-scoped registry and plugin unload lifecycle. The concrete
consumer is Hermes Talk's optional Codex worker; no vendor implementation is added to
Hermes core. `available()` must only check configuration/readiness, never start work.
`open(TaskWorkerRequest)` constructs an inert `TaskWorkerSession`; its `run()` executes
on the existing owning run's worker thread. `cancel()` must signal without blocking.

The linked-child v1 capability gains an additive `external_workers` sub-contract with
version 1 and configured names. A child request may select one such registered name in
`worker`. It cannot supply an executable, workspace, model, policy, or toolset override.
The existing authenticated `/v1/runs` admission still owns exact original input,
idempotency and parent/child persistence. Discord voice execution uses the event-issued
context and control policy in [Discord task contexts](discord-task-context.md). RoomLink
hosted-room grants still cannot dispatch external workers; their independent room/tool
policy is not widened by a Discord voice proof.

The host creates a canonical child, acquires and renews its writer lease, persists the
derived worker goal, and supplies immutable owner/profile/action/origin data. A worker
never runs the parent model. The request's read-only `still_authorized()` callback
checks canonical parent/child/origin retention and must guard external writes. Canonical
owner or exact lease-holder loss irrevocably retires active work. An optional immutable
`room_context` describes the verified Discord audience at admission and grants no text
destination or additional tools. Voice rebind, close, audience changes and proof expiry
do not revoke canonical ownership or cancel accepted work; voice control and delivery
are checked separately. Worker children carry
the standard `_delegate_from` marker, so canonical parent deletion removes their messages
and sessions through the existing deletion path. Worker results may include full output and an artifact
array; failure/cancellation preserves available partial output.

Existing owning-run steering/approval/stop routes remain the only control routes. A
worker exposes its current turn and queues origin-linked corrections under the host's
durable action reservation. Exact parent receipt verification precedes queueing. The
API keeps bounded external approval delivery off the event loop and reports queue acknowledgement, never inferred application. Legacy uncorrelated
steering is refused for external workers. Pending approvals come from the active worker
session and require their exact original request ID; replay, resolve-all and stale
requests cannot acquire new authority. No permanent grants are added.

Disabled/unavailable providers refuse before job/origin admission. Registries have no
cross-profile fallback for task workers. Reconnect/transport recovery belongs to the
provider's original recorded worker identity; host admission never re-launches a consumed
child dispatch. Local implementation and offline fixtures do not establish provider or
production acceptance.
