# Existing Claude Code task delivery

The recipient bridge can address a running native Windows Claude Code task through
its authenticated peer inbox. It does not start a new process, resume the session
elsewhere, or inject terminal keystrokes. Delivery is a next-turn user message;
Claude still applies its peer admission and permission rules.

A Claude task using permission bypass may require approval inside that existing
Claude session before it admits an external Hermes message. Hermes does not
claim bypass, child or self-sent authority and does not change Claude's inbound
policy. Approving the individual incoming message is sufficient when Claude
presents that choice. An absent approval prompt or transcript row cannot prove
whether a message was held, rejected or expired; its receipt remains `unknown`.

## Requirements and identity

Claude must publish peer protocol 1 in its active session registry. The adapter
uses the existing `CLAUDE_CONFIG_DIR`, or the normal `.claude` directory. Discovery
checks the current process owner, exact Windows process creation value, Anthropic
Claude Code executable metadata, session ID and local named-pipe descriptor.
Executable auto-update renames do not invalidate an otherwise identical running
process. Every selection and send checks these facts again.

The peer key remains in Claude's own session directory. Listing and selecting
check file metadata and ACLs without reading the key. An authorized send reads
only `peerToken`, verifies its process/domain binding and checks the connected
pipe's real server PID before transmitting. The adapter never uses `childToken`
or `CLAUDE_CODE_MESSAGING_TOKEN`.

Credential and registry files must be owned by the current operator. Reparse
paths, NULL DACLs and broad Everyone/Users/Guests/Authenticated Users access are
rejected. Existing operator-managed local groups are preserved; this check does
not claim every accepted file has an owner-only ACL. No credential changes or
copies are required.

## Receipts and recovery

The bridge prepares a durable queued operation, then checks fresh authorization
before a single commit. The native message UUID is derived from the bridge's
scope-hashed operation ID, preserving the originally selected target on retries.
A socket write or close does not prove delivery: the receipt stays `unknown`
until the exact UUID, target session, peer origin, original message ID and message
content appear together in that session's transcript. That observation means
`posted`; it does not mean Claude accepted the work or completed a model turn.

Retrying an attempted bridge operation only reconciles its original ID. It does
not resend. Held peer messages, a terminated process, missing history or a receipt
older than the bounded transcript tail can remain `unknown`. Resolve that state
by inspecting the original task; do not submit the same instruction under a new
operation ID merely because the first write had no receipt.

This IPC capability provides `select`, `send` and `reconcile`. Window inspection
and screenshots require separate verified native window support.

## Protocol evidence and verification

The transport follows Anthropic's [cross-session messaging documentation](https://code.claude.com/docs/en/cross-session-messaging).
The authentication line, protocol-1 user envelope and UUID/session preservation
were additionally checked against the locally installed Claude Code 2.1.268
receiver. This is an independent protocol adapter, with no copied Claude source.
Windows pipe-owner validation uses [GetNamedPipeServerProcessId](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getnamedpipeserverprocessid).

Run `scripts/run_tests.sh tests/tools/test_recipient_claude.py -j 1 -q`.
The tests cover ownership, stale session/process binding, fresh authorization,
credential ACLs, ambiguous writes, exact transcript receipts and an owned native
Windows named pipe. They do not send a message to a running Claude task.
