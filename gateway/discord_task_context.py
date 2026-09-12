"""Event-issued Discord voice authority; never infer an operator from HTTP identity fields."""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
import weakref
from dataclasses import dataclass, field

from gateway.config import Platform

REDEEM_SECONDS = 300
LEASE_SECONDS = 900
MAX_SESSION_SECONDS = 8 * 60 * 60
MAX_PROOFS = 1024


class DiscordTaskContextError(ValueError):
    def __init__(self, code="discord_context_denied", status=403):
        self.code, self.status = code, status
        super().__init__(code)


def _identifier(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise DiscordTaskContextError("invalid_discord_binding", 400)
    return value


def reset_voice_context(adapter):
    adapter._task_voice_epoch = secrets.token_hex(16)
    adapter._task_voice_revisions = {}


def note_voice_state(adapter, member, before, after):
    """Run for every Discord voice event, including when core voice is not joined."""
    previous, current = getattr(before, "channel", None), getattr(after, "channel", None)
    if getattr(previous, "id", None) == getattr(current, "id", None):
        return
    revisions = getattr(adapter, "_task_voice_revisions", None)
    if revisions is None:
        revisions = adapter._task_voice_revisions = {}
    for channel in (previous, current):
        if channel is not None:
            key = (member.guild.id, channel.id)
            revisions[key] = revisions.get(key, 0) + 1


def _explicitly_allowed(adapter, member):
    users = {str(value) for value in (getattr(adapter, "_allowed_user_ids", None) or ()) if str(value).isdigit()}
    roles = {str(value) for value in (getattr(adapter, "_allowed_role_ids", None) or ()) if str(value).isdigit()}
    return str(member.id) in users or any(str(role.id) in roles for role in (member.roles or []))


def _snapshot(runner, adapter, guild_id, channel_id, operator_id):
    registered, _ = runner._owning_profile(adapter, Platform.DISCORD)
    client = getattr(adapter, "_client", None)
    if not registered or client is None or client.is_ready() is not True or client.is_closed() is not False:
        raise DiscordTaskContextError("discord_adapter_unavailable", 503)
    guild = client.get_guild(int(guild_id))
    member = guild.get_member(int(operator_id)) if guild else None
    channel = guild.get_channel(int(channel_id)) if guild else None
    if (member is None or getattr(member, "bot", None) is not False or channel is None
            or getattr(getattr(getattr(member, "voice", None), "channel", None), "id", None) != int(channel_id)):
        raise DiscordTaskContextError()
    # Explicit member/role grants only. Open mode, text-channel grants, and shared
    # dashboard credentials are not authority to operate a voice room.
    if not _explicitly_allowed(adapter, member):
        raise DiscordTaskContextError("discord_operator_not_allowlisted")
    states = channel.voice_states
    audience = []
    for user_id, state in states.items():
        if getattr(getattr(state, "channel", None), "id", None) != int(channel_id):
            continue
        peer = guild.get_member(user_id)
        if peer is None or type(getattr(peer, "bot", None)) is not bool:
            raise DiscordTaskContextError("discord_audience_incomplete", 503)
        if not peer.bot:
            if not _explicitly_allowed(adapter, peer):
                raise DiscordTaskContextError("discord_audience_not_allowlisted")
            audience.append(str(peer.id))
    audience = sorted(audience)
    if operator_id not in audience:
        raise DiscordTaskContextError("discord_audience_incomplete", 503)
    epoch = getattr(adapter, "_task_voice_epoch", None)
    if epoch is None:
        epoch = adapter._task_voice_epoch = secrets.token_hex(16)
    revision = (getattr(adapter, "_task_voice_revisions", {}) or {}).get((int(guild_id), int(channel_id)), 0)
    signature = hashlib.sha256(json.dumps([epoch, revision, audience], separators=(",", ":")).encode()).hexdigest()
    return client, {"surface": "discord", "guild_id": guild_id, "channel_id": channel_id,
                    "operator_user_id": operator_id, "audience_revision": signature,
                    "audience_user_ids": audience}


@dataclass
class _Proof:
    adapter: object = field(repr=False)
    client: object = field(repr=False)
    profile: str
    context: dict
    issued_at: float
    redeem_until: float
    owner: str | None = None
    session_id: str | None = None
    binding_id: str | None = None
    lease_until: float | None = None
    retired: bool = False


class DiscordTaskContexts:
    def __init__(self, runner, *, clock=time.time):
        self.runner = weakref.ref(runner)
        self.clock = clock
        self.lock = threading.RLock()
        self.proofs = {}

    def _new(self, adapter, client, profile, context, *, issued_at=None):
        now = self.clock()
        self.proofs = {key: record for key, record in self.proofs.items()
                       if not record.retired and now < (record.lease_until or record.redeem_until)
                       and now < record.issued_at + MAX_SESSION_SECONDS}
        if len(self.proofs) >= MAX_PROOFS:
            raise DiscordTaskContextError("discord_context_capacity", 503)
        token = secrets.token_urlsafe(32)
        record = _Proof(weakref.ref(adapter), weakref.ref(client), profile, context,
                        now if issued_at is None else issued_at, now + REDEEM_SECONDS)
        self.proofs[token] = record
        return {**context, "audience_user_ids": list(context["audience_user_ids"]),
                "profile": profile, "proof": token, "expires_at": record.redeem_until}

    def issue(self, source):
        runner = self.runner()
        owner = runner._transport_owner(source) if runner is not None else None
        if (owner is None or source.platform != Platform.DISCORD
                or getattr(source, "is_bot", False) is not False
                or getattr(source, "delivered_via_upstream_relay", False) is True):
            raise DiscordTaskContextError("discord_event_required")
        adapter, transport_profile = owner
        client = getattr(adapter, "_client", None)
        guild_id, operator_id = str(source.guild_id or ""), str(source.user_id or "")
        if not guild_id.isdigit() or not operator_id.isdigit() or client is None:
            raise DiscordTaskContextError("discord_event_required")
        guild = client.get_guild(int(guild_id))
        member = guild.get_member(int(operator_id)) if guild else None
        channel = getattr(getattr(member, "voice", None), "channel", None)
        if channel is None:
            raise DiscordTaskContextError()
        with self.lock:
            client, context = _snapshot(runner, adapter, guild_id, str(channel.id), operator_id)
            profile = source.profile or transport_profile or getattr(runner, "_primary_profile_name", None) or "default"
            try:
                store = runner.session_store
                key = runner._session_key_for_source(source)
                entry = store.lookup_by_session_key(key)
                if entry is None:
                    entry = store.get_or_create_session(source, touch_activity=False)
                anchor = _identifier(entry.session_id)
            except Exception:
                raise DiscordTaskContextError("discord_anchor_unavailable", 503) from None
            return {**self._new(adapter, client, profile, context), "anchor_session_id": anchor}

    def _current(self, proof, *, profile, owner, session_id=None, binding_id=None, redeem=False):
        _identifier(proof)
        record = self.proofs.get(proof)
        if record is None or record.profile != profile:
            raise DiscordTaskContextError("discord_context_not_found", 404)
        now = self.clock()
        if record.retired or now >= (record.lease_until or record.redeem_until) or now >= record.issued_at + MAX_SESSION_SECONDS:
            record.retired = True
            raise DiscordTaskContextError("discord_context_expired", 409)
        if record.owner is None:
            if not redeem or session_id is None or binding_id is None:
                raise DiscordTaskContextError("discord_context_not_redeemed", 409)
        elif (record.owner != owner or (session_id is not None and record.session_id != session_id)
              or (binding_id is not None and record.binding_id != binding_id)):
            raise DiscordTaskContextError("discord_binding_mismatch", 403)
        runner, adapter = self.runner(), record.adapter()
        try:
            if runner is None or adapter is None:
                raise DiscordTaskContextError()
            client, context = _snapshot(runner, adapter, record.context["guild_id"],
                                        record.context["channel_id"], record.context["operator_user_id"])
            if client is not record.client() or context != record.context:
                raise DiscordTaskContextError("discord_audience_changed", 409)
        except DiscordTaskContextError:
            record.retired = True
            raise
        if record.session_id is None:
            selected, binding = _identifier(session_id), _identifier(binding_id)
            record.session_id, record.binding_id = selected, binding
        record.owner = owner
        record.lease_until = min(now + LEASE_SECONDS, record.issued_at + MAX_SESSION_SECONDS)
        return record

    def verify(self, proof, *, profile, owner, session_id=None, binding_id=None, redeem=False):
        with self.lock:
            record = self._current(proof, profile=profile, owner=owner, session_id=session_id,
                                   binding_id=binding_id, redeem=redeem)
            return {**record.context, "audience_user_ids": list(record.context["audience_user_ids"]),
                    "profile": record.profile, "session_id": record.session_id,
                    "binding_id": record.binding_id, "expires_at": record.lease_until}

    def rebind(self, proof, *, profile, owner, session_id, binding_id, next_session_id, next_binding_id):
        with self.lock:
            record = self._current(proof, profile=profile, owner=owner, session_id=session_id, binding_id=binding_id)
            result = self._new(record.adapter(), record.client(), record.profile, record.context,
                               issued_at=record.issued_at)
            try:
                context = self.verify(result["proof"], profile=profile, owner=owner,
                                      session_id=next_session_id, binding_id=next_binding_id, redeem=True)
            except DiscordTaskContextError:
                self.proofs.pop(result["proof"], None)
                raise
            record.retired = True
            return {**context, "proof": result["proof"]}

    def revoke(self, proof, *, profile, owner, session_id, binding_id):
        with self.lock:
            record = self.proofs.get(_identifier(proof))
            if (record is None or record.profile != profile or record.owner != owner
                    or record.session_id != session_id or record.binding_id != binding_id):
                raise DiscordTaskContextError("discord_context_not_found", 404)
            record.retired = True


def task_contexts(runner):
    if runner is None:
        raise DiscordTaskContextError("discord_adapter_unavailable", 503)
    contexts = getattr(runner, "_discord_task_contexts", None)
    if contexts is None:
        contexts = runner._discord_task_contexts = DiscordTaskContexts(runner)
    return contexts


class GatewayCommandInvocation:
    """Ephemeral capability supplied only to opted-in handlers during gateway dispatch."""
    def __init__(self, runner, source, *, allowed):
        self._runner, self._source = runner, source
        self._active = allowed is True

    def close(self):
        self._active = False

    def capture_discord_task_context_proof(self):
        if not self._active:
            raise DiscordTaskContextError("discord_event_required")
        return task_contexts(self._runner).issue(self._source)
