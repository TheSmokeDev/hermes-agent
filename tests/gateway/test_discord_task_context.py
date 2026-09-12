"""Offline event provenance, audience revisions and configured-peer context boundaries."""
import weakref

import pytest

from gateway.authz_mixin import GatewayAuthorizationMixin
from gateway.config import Platform
from gateway.discord_task_context import (
    DiscordTaskContextError, DiscordTaskContexts, GatewayCommandInvocation, note_voice_state, reset_voice_context,
)
from gateway.platforms.event import MessageEvent
from gateway.run_inbound import GatewayInboundMixin
from gateway.session import SessionSource
from tests.gateway.test_dashboard_consumption import gateway


class Object:
    def __init__(self, **values):
        self.__dict__.update(values)


class Runner(GatewayAuthorizationMixin, GatewayInboundMixin):
    _draining = False
    def _hm_quick_commands(self):
        return {}


def room_fixture():
    channel = Object(id=20)
    guild = Object(id=10, voice_states={})
    members = {}
    def add(user_id, bot=False):
        member = Object(id=user_id, bot=bot, guild=guild, roles=[], voice=Object(channel=channel))
        members[user_id] = member
        guild.voice_states[user_id] = member.voice
        return member
    operator, peer = add(2), add(1)
    add(99, bot=True)
    guild.get_member = members.get
    guild.get_channel = lambda ident: channel if ident == channel.id else None
    client = Object(is_ready=lambda: True, is_closed=lambda: False,
                    get_guild=lambda ident: guild if ident == guild.id else None)
    adapter = Object(_client=client, _allowed_user_ids={"1", "2"}, _allowed_role_ids=set())
    runner = Runner()
    runner.adapters = {}
    runner._profile_adapters = {"alpha": {Platform.DISCORD: adapter}}
    runner._primary_profile_name = "default"
    runner.session_store = Object(lookup_by_session_key=lambda key: Object(session_id="same-session"))
    runner._session_key_for_source = lambda source: "fixture-source-key"
    source = SessionSource(platform=Platform.DISCORD, guild_id="10", chat_id="555", user_id="2", profile="alpha")
    source._transport_adapter_ref = weakref.ref(adapter)
    now = [1000.0]
    contexts = runner._discord_task_contexts = DiscordTaskContexts(runner, clock=lambda: now[0])
    return Object(runner=runner, adapter=adapter, client=client, guild=guild, channel=channel,
                  members=members, operator=operator, peer=peer, add=add, source=source, now=now, contexts=contexts)


def binding(room):
    token = GatewayCommandInvocation(room.runner, room.source, allowed=True).capture_discord_task_context_proof()
    return {"proof": token["proof"], "session_id": "same-session", "binding_id": "native-connection"}


def test_event_only_issuer_scope_lease_and_copy_isolation():
    room = room_fixture()
    invocation = GatewayCommandInvocation(room.runner, room.source, allowed=True)
    captured = invocation.capture_discord_task_context_proof()
    assert captured["surface"] == "discord" and captured["profile"] == "alpha"
    assert captured["anchor_session_id"] == "same-session"
    assert captured["audience_user_ids"] == ["1", "2"] and captured["channel_id"] == "20"
    captured["audience_user_ids"].append("client-change")
    assert room.contexts.proofs[captured["proof"]].context["audience_user_ids"] == ["1", "2"]
    body = {**binding(room)}
    with pytest.raises(DiscordTaskContextError, match="not_redeemed"):
        room.contexts.verify(**body, profile="alpha", owner="owner")
    result = room.contexts.verify(**body, profile="alpha", owner="owner", redeem=True)
    assert result["audience_user_ids"] == ["1", "2"]
    assert (result["guild_id"], result["channel_id"], result["operator_user_id"]) == ("10", "20", "2")
    assert result["channel_id"] != room.source.chat_id
    result["audience_user_ids"].append("forged")
    room.now[0] += 301  # The original five-minute redemption deadline no longer ends the live call.
    fresh = room.contexts.verify(**body, profile="alpha", owner="owner")
    assert fresh["audience_user_ids"] == ["1", "2"] and fresh["expires_at"] > result["expires_at"]
    invocation.close()
    with pytest.raises(DiscordTaskContextError, match="event_required"):
        invocation.capture_discord_task_context_proof()
    forged = SessionSource.from_dict(room.source.to_dict())
    with pytest.raises(DiscordTaskContextError, match="event_required"):
        room.contexts.issue(forged)


@pytest.mark.parametrize("change", ["member", "role", "open", "missing_member", "unknown_audience", "not_ready", "foreign_adapter", "relay"])
def test_issuance_requires_live_explicit_operator_and_complete_audience(change):
    room = room_fixture()
    if change == "member": room.operator.voice = None
    elif change == "role":
        room.adapter._allowed_user_ids = set()
        room.adapter._allowed_role_ids = {17}
        room.operator.roles = [Object(id=18)]
    elif change == "open": room.adapter._allowed_user_ids = {"*"}
    elif change == "missing_member": room.members.pop(2)
    elif change == "unknown_audience": room.members.pop(1)
    elif change == "not_ready": room.client.is_ready = lambda: False
    elif change == "foreign_adapter": room.runner._profile_adapters = {}
    elif change == "relay": room.source.delivered_via_upstream_relay = True
    with pytest.raises(DiscordTaskContextError): room.contexts.issue(room.source)


def test_actual_member_role_allows_issuance_without_cross_guild_roles():
    room = room_fixture()
    room.adapter._allowed_user_ids = set()
    room.adapter._allowed_role_ids = {17}
    room.operator.roles = [Object(id=17)]
    room.peer.roles = [Object(id=17)]
    assert room.contexts.issue(room.source)["proof"]


@pytest.mark.parametrize("mutation", ["join", "leave_rejoin", "operator_move", "allowlist", "reconnect", "same_client_reconnect"])
def test_audience_or_operator_mutation_permanently_retires_proof(mutation):
    room = room_fixture()
    body = binding(room)
    room.contexts.verify(**body, profile="alpha", owner="owner", redeem=True)
    if mutation == "join": room.add(3)
    elif mutation == "leave_rejoin":
        note_voice_state(room.adapter, room.peer, room.peer.voice, Object(channel=None))
        note_voice_state(room.adapter, room.peer, Object(channel=None), room.peer.voice)
    elif mutation == "operator_move": room.operator.voice = Object(channel=Object(id=21))
    elif mutation == "allowlist": room.adapter._allowed_user_ids = set()
    elif mutation == "reconnect": room.adapter._client = Object(**room.client.__dict__)
    elif mutation == "same_client_reconnect": reset_voice_context(room.adapter)
    with pytest.raises(DiscordTaskContextError): room.contexts.verify(**body, profile="alpha", owner="owner")
    room.adapter._allowed_user_ids = {"2"}
    room.operator.voice = Object(channel=room.channel)
    room.adapter._client = room.client
    room.members.pop(3, None)
    room.guild.voice_states.pop(3, None)
    with pytest.raises(DiscordTaskContextError, match="expired"):
        room.contexts.verify(**body, profile="alpha", owner="owner")


@pytest.mark.parametrize("field,value", [("profile", "beta"), ("owner", "foreign"), ("session_id", "other"), ("binding_id", "other")])
def test_binding_scope_refusals_do_not_retire_legitimate_owner(field, value):
    room = room_fixture()
    args = {**binding(room), "profile": "alpha", "owner": "owner"}
    room.contexts.verify(**args, redeem=True)
    with pytest.raises(DiscordTaskContextError): room.contexts.verify(**{**args, field: value})
    assert room.contexts.verify(**args)["binding_id"] == "native-connection"


def test_expiry_rebind_and_revoke_have_no_background_job_authority():
    room = room_fixture()
    unused = binding(room)
    room.now[0] += 301
    with pytest.raises(DiscordTaskContextError, match="expired"):
        room.contexts.verify(**unused, profile="alpha", owner="owner", redeem=True)
    args = {**binding(room), "profile": "alpha", "owner": "owner"}
    room.contexts.verify(**args, redeem=True)
    rebound = room.contexts.rebind(**args, next_session_id="new-target", next_binding_id="next-native")
    assert rebound["proof"] != args["proof"] and rebound["session_id"] == "new-target"
    with pytest.raises(DiscordTaskContextError): room.contexts.verify(**args)
    current = {key: rebound[key] for key in ("proof", "session_id", "binding_id")}
    room.contexts.revoke(**current, profile="alpha", owner="owner")
    with pytest.raises(DiscordTaskContextError): room.contexts.verify(**current, profile="alpha", owner="owner")


@pytest.mark.asyncio
async def test_real_plugin_registration_and_dispatch_supply_ephemeral_context(tmp_path, monkeypatch):
    from hermes_cli import plugins
    home = tmp_path / "home"
    path = home / "plugins" / "context-fixture"
    path.mkdir(parents=True)
    (path / "plugin.yaml").write_text("name: context-fixture\nversion: 1.0.0\ndescription: fixture\n")
    (path / "__init__.py").write_text('captured = []\nasync def handler(raw_args, invocation=None):\n    captured.append(invocation)\n    return invocation.capture_discord_task_context_proof()["proof"]\ndef register(ctx):\n    ctx.register_command("context-fixture", handler=handler, invocation_context=True)\n')
    (home / "config.yaml").write_text("plugins:\n  enabled: [context-fixture]\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = plugins.PluginManager()
    manager.discover_and_load()
    monkeypatch.setattr(plugins, "_ensure_plugins_discovered", lambda: manager)
    room = room_fixture()
    event = MessageEvent(text="/context-fixture join", source=room.source, message_id="event-one")
    handled, proof, command = await room.runner._hm_dispatch_quick_and_plugin_commands(event, room.source, "context-fixture")
    assert handled and command == "context-fixture" and proof in room.contexts.proofs
    callback = manager._plugin_commands["context-fixture"]["handler"]
    with pytest.raises(DiscordTaskContextError, match="event_required"):
        callback.__globals__["captured"][0].capture_discord_task_context_proof()
    manager.unload()


@pytest.mark.asyncio
async def test_http_requires_proof_profile_session_and_immutable_binding(tmp_path, monkeypatch):
    room = room_fixture()
    async with gateway(tmp_path, monkeypatch) as (_, keys, stores, adapter, client):
        room.runner.config = adapter.gateway_runner.config
        adapter.gateway_runner = room.runner
        body = binding(room)
        base = "/p/alpha/v1/task-context/discord/"
        auth = {"Authorization": "Bearer " + keys["alpha"]}
        assert (await client.post(base + "redeem", json=body)).status == 401
        assert (await client.post(base + "redeem", headers=auth, json={
            "surface": "discord", "guild_id": "10", "channel_id": "20", "operator_user_id": "2"})).status == 400
        assert (await client.post(base + "redeem", headers=auth, json={**body, "proof": "forged"})).status == 404
        beta = {"Authorization": "Bearer " + keys["beta"]}
        assert (await client.post(base.replace("alpha", "beta") + "redeem", headers=beta, json=body)).status == 404
        for invalid in (None, "", [], "with space"):
            assert (await client.post(base + "redeem", headers=auth, json={**body, "binding_id": invalid})).status == 400
        response = await client.post(base + "redeem", headers=auth, json=body)
        assert response.status == 200, await response.text()
        context = await response.json()
        assert context["audience_user_ids"] == ["1", "2"] and "proof" not in context
        assert response.headers["Cache-Control"] == "no-store"
        assert (await client.post(base + "verify", headers=auth, json={**body, "binding_id": "other"})).status == 403
        assert (await client.post(base + "verify", headers=auth, json=body)).status == 200
        assert (await client.post(base + "verify", headers=auth, json={**body, "binding_id": None})).status == 400
        stores["alpha"].create_session("new-target", source="test")
        moved = await client.post(base + "rebind", headers=auth, json={**body,
            "next_session_id": "new-target", "next_binding_id": "next-native"})
        assert moved.status == 200, await moved.text()
        next_body = {key: (await moved.json())[key] for key in body}
        assert (await client.post(base + "verify", headers=auth, json=body)).status == 409
        room.add(3)
        assert (await client.post(base + "verify", headers=auth, json=next_body)).status == 403
        assert (await client.post(base + "revoke", headers=auth, json=next_body)).status == 200
        assert stores["alpha"].get_messages("same-session") == []


@pytest.mark.parametrize("users,roles,peer_roles", [({1, 2}, set(), []), ({"2"}, {17}, [Object(id=17)]), ({"2"}, {"17"}, [Object(id=17)])])
def test_every_listener_requires_explicit_adapter_user_or_role_grant(users, roles, peer_roles):
    room = room_fixture()
    room.adapter._allowed_user_ids = users
    room.adapter._allowed_role_ids = roles
    room.peer.roles = peer_roles
    context = room.contexts.verify(**binding(room), profile="alpha", owner="owner", redeem=True)
    assert context["audience_user_ids"] == ["1", "2"]
    room.adapter._allowed_user_ids = {"2", "*"}
    room.peer.roles = []
    with pytest.raises(DiscordTaskContextError, match="audience_not_allowlisted"):
        room.contexts.issue(room.source)


@pytest.mark.parametrize("existing", [False, True])
def test_anchor_uses_canonical_profile_routing_and_preserves_existing_history(tmp_path, monkeypatch, existing):
    import hermes_state
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session import SessionStore
    from hermes_state import SessionDB
    root = tmp_path / "hermes"
    profile = root / "profiles" / "alpha"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)
    room = room_fixture()
    store = room.runner.session_store = SessionStore(root / "sessions", GatewayConfig(multiplex_profiles=True))
    room.runner._session_key_for_source = GatewayRunner._session_key_for_source.__get__(room.runner)
    key = room.runner._session_key_for_source(room.source)
    try:
        if existing:
            entry = store.get_or_create_session(room.source, touch_activity=False)
            with SessionDB(profile / "state.db") as db:
                db.append_message(entry.session_id, "user", "Earlier canonical text")
            original = (entry.session_id, entry.updated_at)
        else:
            assert store.lookup_by_session_key(key) is None
            room.adapter._allowed_user_ids = {"2"}
            with pytest.raises(DiscordTaskContextError, match="audience_not_allowlisted"):
                room.contexts.issue(room.source)
            assert store.lookup_by_session_key(key) is None
            room.adapter._allowed_user_ids = {"1", "2"}
        captured = room.contexts.issue(room.source)
        entry = store.lookup_by_session_key(key)
        assert entry.session_id == captured["anchor_session_id"] and captured["profile"] == "alpha"
        assert room.contexts.issue(room.source)["anchor_session_id"] == entry.session_id
        with SessionDB(profile / "state.db") as db:
            assert db.get_session(entry.session_id) is not None
            assert [row["content"] for row in db.get_messages(entry.session_id)] == (["Earlier canonical text"] if existing else [])
        if existing:
            assert (entry.session_id, entry.updated_at) == original
        with SessionDB(root / "state.db") as db:
            assert db.get_session(entry.session_id) is None
    finally:
        store.close_all_db_handles()
