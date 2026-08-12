from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.realtime_voice_admission import RealtimeSessionBinding, RealtimeUtterance
from agent.realtime_voice_provider import (
    RealtimeCapability,
    RealtimeOutputAudioFormat,
    RealtimeTool,
    RealtimeVoiceEvent,
    RealtimeVoiceProvider,
    RealtimeVoiceSession,
    RealtimeVoiceSetup,
)
from agent.realtime_voice_registry import _reset_for_tests, register_provider
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, SendResult
from gateway.session import SessionEntry, SessionSource, build_session_key


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel-1",
        chat_type="group",
        user_id="123456789",
        thread_id="thread-1",
        scope_id="111",
    )


def _host_fixture():
    from gateway.run import GatewayRunner
    from gateway.realtime_voice_invocation import (
        _invoke_plugin_command_with_context,
        _register_gateway_runner,
    )
    from plugins.platforms.discord.adapter import DiscordAdapter

    source = _source()
    route = build_session_key(source)
    entry = SessionEntry(
        session_key=route,
        session_id="durable-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.DISCORD,
        chat_type="group",
    )
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True)}
    )
    runner.session_store = MagicMock()
    runner.session_store.get_exact_session_entry_snapshot.return_value = (entry, 3)
    runner._running_agents = {}
    runner._is_session_running = lambda key: key in runner._running_agents
    adapter = DiscordAdapter(runner.config.platforms[Platform.DISCORD])
    adapter.gateway_runner = runner
    adapter.set_message_handler(lambda event: runner._handle_message(event))

    async def send_success(**_kwargs):
        return SendResult(success=True, message_id="delivered")

    adapter._send_with_retry = send_success
    runner.adapters = {Platform.DISCORD: adapter}
    _register_gateway_runner(runner)

    captured = []

    async def capture():
        await _invoke_plugin_command_with_context(
            runner=runner,
            handler=lambda _args, invocation: captured.append(
                invocation.capture_realtime_voice_attachment_factory()
            ),
            raw_args="core join",
            source=source,
            routing_key=route,
            authenticated=True,
            internal=False,
        )

    return runner, source, entry, captured, capture


def _binding(route: str) -> RealtimeSessionBinding:
    return RealtimeSessionBinding(
        profile_id="default",
        routing_key=route,
        runtime_session_id=route,
        durable_session_id="durable-1",
        provider_session_id="provider-attachment-1",
        selection_generation=3,
    )


def _utterance(text: str = "hello from voice") -> RealtimeUtterance:
    return RealtimeUtterance(
        provider_session_id="provider-attachment-1",
        provider_turn_id="turn-1",
        item_id="item-1",
        text=text,
        received_at=1.0,
    )


def _native_output_format() -> RealtimeOutputAudioFormat:
    return RealtimeOutputAudioFormat(
        mime_type="audio/pcm",
        sample_rate_hz=24000,
        channels=1,
        sample_encoding="pcm_s16le",
        sample_width_bytes=2,
        endianness="little",
    )


@pytest.mark.asyncio
async def test_reserve_native_output_uses_exact_persisted_text_and_live_adapter_authority():
    import hashlib

    from gateway.realtime_voice_messaging_host import (
        GatewayRealtimeVoiceMessagingHost,
        RealtimeVoiceFinalizationReceipt,
    )

    runner, source, _entry, captured, capture = _host_fixture()
    await capture()
    marker = "turn-marker"
    rows = [
        {
            "id": 10,
            "role": "user",
            "content": "hello from voice",
            "display_metadata": {"realtime_voice_turn_marker": marker},
        },
        {
            "id": 11,
            "role": "assistant",
            "content": "canonical answer",
            "tool_calls": [],
        },
    ]
    runner._session_db = SimpleNamespace(
        _db=MagicMock(get_messages=MagicMock(return_value=rows))
    )
    adapter = runner.adapters[Platform.DISCORD]
    adapter._voice_clients[111] = SimpleNamespace(is_connected=lambda: True)
    adapter._voice_connection_generations[111] = 4
    adapter._voice_mixer_generations[111] = 7
    host = GatewayRealtimeVoiceMessagingHost(
        captured[0], runner, output_audio_format=_native_output_format()
    )
    receipt = RealtimeVoiceFinalizationReceipt("durable-1", marker, 10, 11)
    host._finalizations.add(receipt)

    request = await host.reserve_native_output(
        _binding(build_session_key(source)), receipt
    )
    reservation = request.reservation

    assert request.canonical_text == "canonical answer"
    assert not hasattr(reservation, "canonical_text")
    assert request.content_digest == hashlib.sha256(b"canonical answer").hexdigest()
    assert request.output_audio_format is host._output_audio_format
    assert reservation.guild_id == 111
    assert reservation.connection_generation == 4
    assert reservation.mixer_generation == 7
    assert reservation.selection_generation == 3
    lease = object()
    adapter.acquire_native_playback_lease = AsyncMock(return_value=lease)
    assert (
        await host.acquire_native_playback(
            reservation,
            lease_id="lease-1",
            response_id="response-1",
            transport_generation=3,
        )
        is lease
    )
    adapter.acquire_native_playback_lease.assert_awaited_once_with(
        111,
        "lease-1",
        "response-1",
        marker,
        4,
        7,
        input_format="pcm_s16le",
        sample_rate=24000,
        channels=1,
    )
    with pytest.raises(PermissionError, match="consumed"):
        await host.reserve_native_output(_binding(build_session_key(source)), receipt)


@pytest.mark.asyncio
async def test_failed_durable_lookup_consumes_finalization_receipt_once():
    from gateway.realtime_voice_messaging_host import (
        GatewayRealtimeVoiceMessagingHost,
        RealtimeVoiceFinalizationReceipt,
    )

    runner, source, _entry, captured, capture = _host_fixture()
    await capture()
    db = MagicMock(get_messages=MagicMock(side_effect=RuntimeError("db unavailable")))
    runner._session_db = SimpleNamespace(_db=db)
    host = GatewayRealtimeVoiceMessagingHost(
        captured[0], runner, output_audio_format=_native_output_format()
    )
    receipt = RealtimeVoiceFinalizationReceipt("durable-1", "marker", 1, 2)
    host._finalizations.add(receipt)

    with pytest.raises(RuntimeError, match="db unavailable"):
        await host.reserve_native_output(_binding(build_session_key(source)), receipt)
    with pytest.raises(PermissionError, match="consumed"):
        await host.reserve_native_output(_binding(build_session_key(source)), receipt)
    db.get_messages.assert_called_once()


@pytest.mark.asyncio
async def test_reservation_rejects_coercive_durable_row_ids():
    from gateway.realtime_voice_messaging_host import (
        GatewayRealtimeVoiceMessagingHost,
        RealtimeVoiceFinalizationReceipt,
    )

    runner, source, _entry, captured, capture = _host_fixture()
    await capture()
    marker = "marker"
    runner._session_db = SimpleNamespace(
        _db=MagicMock(
            get_messages=MagicMock(
                return_value=[
                    {
                        "id": True,
                        "role": "user",
                        "content": "voice",
                        "display_metadata": {
                            "realtime_voice_turn_marker": marker
                        },
                    },
                    {"id": 2, "role": "assistant", "content": "answer"},
                ]
            )
        )
    )
    host = GatewayRealtimeVoiceMessagingHost(
        captured[0], runner, output_audio_format=_native_output_format()
    )
    receipt = RealtimeVoiceFinalizationReceipt("durable-1", marker, 1, 2)
    host._finalizations.add(receipt)

    with pytest.raises(PermissionError, match="rows"):
        await host.reserve_native_output(_binding(build_session_key(source)), receipt)


@pytest.mark.asyncio
async def test_cancelled_acquisition_waiter_cannot_orphan_late_lease():
    from gateway.realtime_voice_messaging_host import (
        GatewayRealtimeVoiceMessagingHost,
        RealtimeVoiceFinalizationReceipt,
    )

    runner, source, _entry, captured, capture = _host_fixture()
    await capture()
    marker = "marker"
    runner._session_db = SimpleNamespace(_db=MagicMock(get_messages=MagicMock(return_value=[
        {"id": 1, "role": "user", "content": "voice", "display_metadata": {"realtime_voice_turn_marker": marker}},
        {"id": 2, "role": "assistant", "content": "answer", "tool_calls": []},
    ])))
    adapter = runner.adapters[Platform.DISCORD]
    adapter._voice_clients[111] = SimpleNamespace(is_connected=lambda: True)
    adapter._voice_connection_generations[111] = 4
    adapter._voice_mixer_generations[111] = 7
    host = GatewayRealtimeVoiceMessagingHost(captured[0], runner, output_audio_format=_native_output_format())
    receipt = RealtimeVoiceFinalizationReceipt("durable-1", marker, 1, 2)
    host._finalizations.add(receipt)
    request = await host.reserve_native_output(_binding(build_session_key(source)), receipt)
    entered = asyncio.Event()
    release = asyncio.Event()
    lease = SimpleNamespace(close=AsyncMock())

    async def acquire(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return lease

    adapter.acquire_native_playback_lease = acquire
    waiter = asyncio.create_task(host.acquire_native_playback(
        request, lease_id="lease", response_id="response", transport_generation=1,
    ))
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    closer = asyncio.create_task(host.close_attachment(None))
    release.set()
    await closer
    lease.close.assert_awaited_once()
    with pytest.raises(PermissionError, match="forged or stale|already acquired"):
        await host.acquire_native_playback(
            request, lease_id="lease-2", response_id="response-2", transport_generation=1,
        )


@pytest.mark.asyncio
async def test_hostile_finalization_receipt_is_rejected_without_hashing():
    from gateway.realtime_voice_messaging_host import GatewayRealtimeVoiceMessagingHost

    class Hostile:
        def __hash__(self):
            raise AssertionError("hostile hash executed")

    runner, source, _entry, captured, capture = _host_fixture()
    await capture()
    runner._session_db = SimpleNamespace(_db=MagicMock())
    host = GatewayRealtimeVoiceMessagingHost(captured[0], runner, output_audio_format=_native_output_format())
    for hostile in ([], Hostile()):
        with pytest.raises(PermissionError, match="not minted"):
            await host.reserve_native_output(_binding(build_session_key(source)), hostile)


@pytest.mark.asyncio
async def test_closed_host_cannot_publish_new_permit():
    from gateway.realtime_voice_messaging_host import _create_messaging_host

    runner, source, _entry, captured, capture = _host_fixture()
    await capture()
    runner._session_db = SimpleNamespace(
        _db=MagicMock(get_messages=MagicMock(return_value=[]))
    )
    host = _create_messaging_host(captured[0], runner)
    binding = _binding(build_session_key(source))
    await host.close_attachment(binding)

    assert await host.authorize(binding, _utterance()) is None
    assert not host._permits


def test_magic_mock_event_without_installed_claim_is_ordinary():
    from gateway.realtime_voice_messaging_host import _CLAIM_ATTR, _claim_for

    event = MagicMock()
    assert _CLAIM_ATTR not in vars(event)

    assert _claim_for(object(), event) is None


def test_explicit_noncanonical_claim_remains_rejected():
    from gateway.realtime_voice_messaging_host import (
        _CLAIM_ATTR,
        RealtimeVoiceIngressError,
        _claim_for,
    )

    event = MagicMock()
    setattr(event, _CLAIM_ATTR, object())

    with pytest.raises(RealtimeVoiceIngressError, match="invalid canonical realtime claim"):
        _claim_for(object(), event)


@pytest.mark.asyncio
async def test_exact_permit_enters_canonical_handler_once_and_returns_durable_receipt():
    from gateway.realtime_voice_messaging_host import (
        RealtimeVoiceFinalizationReceipt,
        _commit_realtime_voice_slot_claim,
        _create_messaging_host,
        _finalize_realtime_voice_event,
        _preflight_realtime_voice_event,
        _prepare_realtime_voice_slot_claim,
        _validate_realtime_voice_event_after_resolution,
    )

    runner, source, entry, captured, capture = _host_fixture()
    await capture()
    route = build_session_key(source)
    db = MagicMock()
    rows = []
    completed_rows = [
        {
            "id": 10,
            "role": "user",
            "content": "hello from voice",
            "display_metadata": None,
        },
        {
            "id": 11,
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call-1"}],
            "display_metadata": None,
        },
        {"id": 12, "role": "tool", "content": "inert", "display_metadata": None},
        {
            "id": 13,
            "role": "assistant",
            "content": "canonical answer",
            "tool_calls": [],
            "display_metadata": None,
        },
        {
            "id": 14,
            "role": "session_meta",
            "content": None,
            "display_metadata": None,
        },
    ]
    db.get_messages.side_effect = lambda *_args, **_kwargs: list(rows)

    def stamp(*_args, **kwargs):
        completed_rows[0]["display_metadata"] = kwargs["display_metadata"]
        return True

    db.set_message_display_kind.side_effect = stamp
    runner._session_db = SimpleNamespace(_db=db)
    seen: list[MessageEvent] = []

    async def canonical(event: MessageEvent):
        seen.append(event)
        assert _preflight_realtime_voice_event(runner, event, route)
        slot_claim = _prepare_realtime_voice_slot_claim(runner, event, route)
        _commit_realtime_voice_slot_claim(runner, event, route, slot_claim)
        assert _validate_realtime_voice_event_after_resolution(runner, event, entry)
        rows.extend(completed_rows)
        await _finalize_realtime_voice_event(runner, event, entry.session_id)
        return "canonical answer"

    runner._handle_message = canonical
    host = _create_messaging_host(captured[0], runner)
    binding = _binding(route)
    utterance = _utterance()

    permit = await host.authorize(binding, utterance)
    receipt = await host.submit(binding, utterance, permit)

    assert len(seen) == 1
    assert seen[0].text == utterance.text
    assert seen[0].source is not source
    assert receipt.durable_session_id == entry.session_id
    assert receipt.user_message_id == 10
    assert receipt.assistant_message_id == 13
    assert host.validate_finalization(receipt)
    forged_equal_receipt = RealtimeVoiceFinalizationReceipt(
        durable_session_id=receipt.durable_session_id,
        turn_marker=receipt.turn_marker,
        user_message_id=receipt.user_message_id,
        assistant_message_id=receipt.assistant_message_id,
    )
    assert not host.validate_finalization(forged_equal_receipt)
    db.set_message_display_kind.assert_called_once()
    assert db.set_message_display_kind.call_args.args[:2] == (entry.session_id, 10)

    completed_rows[0]["content"] = "different persisted user row"
    with pytest.raises(PermissionError, match="accepted utterance"):
        await _finalize_realtime_voice_event(runner, seen[0], entry.session_id)

    with pytest.raises(PermissionError, match="consumed"):
        await host.submit(binding, utterance, permit)


@pytest.mark.asyncio
async def test_field_equivalent_resolved_entry_is_rejected_before_canonical_work():
    from dataclasses import replace

    from gateway.realtime_voice_messaging_host import (
        _commit_realtime_voice_slot_claim,
        _create_messaging_host,
        _preflight_realtime_voice_event,
        _prepare_realtime_voice_slot_claim,
        _validate_realtime_voice_event_after_resolution,
    )

    runner, source, entry, captured, capture = _host_fixture()
    await capture()
    route = build_session_key(source)
    runner._session_db = SimpleNamespace(
        _db=MagicMock(get_messages=MagicMock(return_value=[]))
    )

    async def canonical(event: MessageEvent):
        assert _preflight_realtime_voice_event(runner, event, route)
        slot_claim = _prepare_realtime_voice_slot_claim(runner, event, route)
        _commit_realtime_voice_slot_claim(runner, event, route, slot_claim)
        copied_entry = replace(entry)
        assert copied_entry is not entry
        assert copied_entry == entry
        _validate_realtime_voice_event_after_resolution(runner, event, copied_entry)

    runner._handle_message = canonical
    host = _create_messaging_host(captured[0], runner)
    binding = _binding(route)
    utterance = _utterance()
    permit = await host.authorize(binding, utterance)

    with pytest.raises(PermissionError, match="exact captured session entry"):
        await host.submit(binding, utterance, permit)


@pytest.mark.asyncio
async def test_legitimate_event_rewrite_preserves_and_revalidates_exact_claim():
    from gateway.realtime_voice_messaging_host import (
        _commit_realtime_voice_slot_claim,
        _create_messaging_host,
        _preflight_realtime_voice_event,
        _prepare_realtime_voice_slot_claim,
        _rewrite_realtime_voice_event,
        _validate_realtime_voice_event_after_resolution,
    )

    runner, source, entry, captured, capture = _host_fixture()
    await capture()
    route = build_session_key(source)
    runner._session_db = SimpleNamespace(
        _db=MagicMock(get_messages=MagicMock(return_value=[]))
    )
    seen = []

    async def canonical(event: MessageEvent):
        rewritten = _rewrite_realtime_voice_event(runner, event, "rewritten voice")
        seen.append(rewritten)
        assert _preflight_realtime_voice_event(runner, rewritten, route)
        slot_claim = _prepare_realtime_voice_slot_claim(runner, rewritten, route)
        _commit_realtime_voice_slot_claim(runner, rewritten, route, slot_claim)
        assert _validate_realtime_voice_event_after_resolution(runner, rewritten, entry)
        raise RuntimeError("bounded stop after claim validation")

    runner._handle_message = canonical
    host = _create_messaging_host(captured[0], runner)
    binding = _binding(route)
    utterance = _utterance()
    permit = await host.authorize(binding, utterance)

    with pytest.raises(RuntimeError, match="bounded stop"):
        await host.submit(binding, utterance, permit)
    assert seen[0].text == "rewritten voice"


@pytest.mark.asyncio
async def test_busy_route_rejects_before_canonical_handler_or_queue_mutation():
    from gateway.realtime_voice_messaging_host import _create_messaging_host

    runner, source, _entry, captured, capture = _host_fixture()
    await capture()
    route = build_session_key(source)
    runner._running_agents[route] = object()
    runner._handle_message = MagicMock()
    host = _create_messaging_host(captured[0], runner)

    assert await host.authorize(_binding(route), _utterance()) is None
    runner._handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_route_becoming_busy_after_authorize_rejects_before_handler_entry():
    from gateway.realtime_voice_messaging_host import _create_messaging_host

    runner, source, _entry, captured, capture = _host_fixture()
    await capture()
    route = build_session_key(source)
    runner._session_db = SimpleNamespace(
        _db=MagicMock(get_messages=MagicMock(return_value=[]))
    )
    runner._handle_message = MagicMock()
    host = _create_messaging_host(captured[0], runner)
    binding = _binding(route)
    utterance = _utterance()
    permit = await host.authorize(binding, utterance)

    runner._running_agents[route] = object()
    with pytest.raises(PermissionError, match="busy"):
        await host.submit(binding, utterance, permit)

    runner._handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_submit_rechecks_route_busy_after_authorize_before_handler_entry():
    from gateway.realtime_voice_messaging_host import _create_messaging_host

    runner, source, _entry, captured, capture = _host_fixture()
    await capture()
    route = build_session_key(source)
    runner._session_db = SimpleNamespace(
        _db=MagicMock(get_messages=MagicMock(return_value=[]))
    )
    runner._handle_message = MagicMock()
    host = _create_messaging_host(captured[0], runner)
    binding = _binding(route)
    utterance = _utterance()
    permit = await host.authorize(binding, utterance)
    runner._running_agents[route] = object()

    with pytest.raises(PermissionError, match="busy"):
        await host.submit(binding, utterance, permit)
    runner._handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_provider_transcript_cannot_become_gateway_control_input():
    from gateway.realtime_voice_messaging_host import _create_messaging_host

    runner, source, _entry, captured, capture = _host_fixture()
    await capture()
    runner._session_db = SimpleNamespace(
        _db=MagicMock(get_messages=MagicMock(return_value=[]))
    )
    runner._handle_message = MagicMock()
    host = _create_messaging_host(captured[0], runner)

    assert (
        await host.authorize(_binding(build_session_key(source)), _utterance("/new"))
        is None
    )
    runner._handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_accepted_canonical_work_survives_submit_waiter_cancellation():
    from gateway.realtime_voice_messaging_host import (
        _commit_realtime_voice_slot_claim,
        _create_messaging_host,
        _finalize_realtime_voice_event,
        _preflight_realtime_voice_event,
        _prepare_realtime_voice_slot_claim,
        _validate_realtime_voice_event_after_resolution,
    )

    runner, source, entry, captured, capture = _host_fixture()
    await capture()
    route = build_session_key(source)
    db = MagicMock()
    rows = []
    completed_rows = [
        {
            "id": 20,
            "role": "user",
            "content": "hello from voice",
            "display_metadata": None,
        },
        {"id": 21, "role": "assistant", "content": "done", "display_metadata": None},
    ]
    db.get_messages.side_effect = lambda *_args, **_kwargs: list(rows)

    def stamp(*_args, **kwargs):
        completed_rows[0]["display_metadata"] = kwargs["display_metadata"]
        return True

    db.set_message_display_kind.side_effect = stamp
    runner._session_db = SimpleNamespace(_db=db)
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def canonical(event: MessageEvent):
        started.set()
        await release.wait()
        assert _preflight_realtime_voice_event(runner, event, route)
        slot_claim = _prepare_realtime_voice_slot_claim(runner, event, route)
        _commit_realtime_voice_slot_claim(runner, event, route, slot_claim)
        assert _validate_realtime_voice_event_after_resolution(runner, event, entry)
        rows.extend(completed_rows)
        await _finalize_realtime_voice_event(runner, event, entry.session_id)
        completed.set()
        return {"final_response": "done"}

    runner._handle_message = canonical
    host = _create_messaging_host(captured[0], runner)
    binding = _binding(route)
    utterance = _utterance()
    permit = await host.authorize(binding, utterance)
    waiter = asyncio.create_task(host.submit(binding, utterance, permit))
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await asyncio.wait_for(completed.wait(), timeout=1)


class _Session(RealtimeVoiceSession):
    def __init__(self):
        super().__init__(frozenset())
        self.closed = 0
        self.stop = asyncio.Event()

    async def send_audio(self, audio: bytes, *, mime_type: str | None = None) -> None:
        return None

    async def _submit_tool_results(self, batch_id, results) -> None:
        raise AssertionError("provider tools are inert")

    async def _events(self) -> AsyncIterator[RealtimeVoiceEvent]:
        await self.stop.wait()
        if False:
            yield  # pragma: no cover

    async def _close(self) -> None:
        self.closed += 1
        self.stop.set()


class _Provider(RealtimeVoiceProvider):
    def __init__(self, session: _Session):
        self.session = session
        self.opened = 0

    @property
    def name(self) -> str:
        return "messaging-host-fake"

    async def open_session(self, setup: RealtimeVoiceSetup) -> RealtimeVoiceSession:
        self.opened += 1
        return self.session


@pytest.mark.asyncio
async def test_factory_composes_one_controller_provider_and_idempotent_teardown():
    _reset_for_tests()
    runner, source, _entry, captured, capture = _host_fixture()
    await capture()
    runner._session_db = SimpleNamespace(
        _db=MagicMock(get_messages=MagicMock(return_value=[]))
    )
    session = _Session()
    provider = _Provider(session)
    assert register_provider(provider)
    try:
        output_format = _native_output_format()
        attachment = await captured[0].open(
            provider.name,
            RealtimeVoiceSetup(output_audio=output_format),
            provider_session_id="provider-attachment-1",
        )
        assert attachment.binding.routing_key == build_session_key(source)
        assert attachment.binding.durable_session_id == "durable-1"
        assert provider.opened == 1
        assert attachment._controller._host._output_audio_format is output_format

        await attachment.close()
        await attachment.close()
        assert session.closed == 1
    finally:
        _reset_for_tests()


@pytest.mark.asyncio
async def test_factory_open_is_consumed_once_even_after_attachment_closes():
    _reset_for_tests()
    runner, _source_value, _entry, captured, capture = _host_fixture()
    await capture()
    runner._session_db = SimpleNamespace(
        _db=MagicMock(get_messages=MagicMock(return_value=[]))
    )
    first_session = _Session()
    provider = _Provider(first_session)
    assert register_provider(provider)
    try:
        attachment = await captured[0].open(
            provider.name,
            RealtimeVoiceSetup(),
            provider_session_id="provider-attachment-1",
        )
        await attachment.close()

        with pytest.raises(PermissionError, match="already consumed"):
            await captured[0].open(
                provider.name,
                RealtimeVoiceSetup(),
                provider_session_id="provider-attachment-2",
            )
        assert provider.opened == 1
    finally:
        _reset_for_tests()


@pytest.mark.asyncio
async def test_factory_open_enforces_caller_required_capabilities():
    _reset_for_tests()
    _runner, _source_value, _entry, captured, capture = _host_fixture()
    await capture()
    session = _Session()
    provider = _Provider(session)
    assert register_provider(provider)
    try:
        with pytest.raises(Exception, match="input_commit_events"):
            await captured[0].open(
                provider.name,
                RealtimeVoiceSetup(),
                provider_session_id="provider-attachment-1",
                required_capabilities=frozenset({
                    RealtimeCapability.INPUT_COMMIT_EVENTS
                }),
            )
        assert provider.opened == 0
    finally:
        _reset_for_tests()


@pytest.mark.asyncio
async def test_factory_rejects_provider_tools_before_provider_open():
    _reset_for_tests()
    _runner, _source_value, _entry, captured, capture = _host_fixture()
    await capture()
    session = _Session()
    provider = _Provider(session)
    assert register_provider(provider)
    setup = RealtimeVoiceSetup(
        tools=(
            RealtimeTool(
                name="forbidden_tool",
                description="must stay inert",
                parameters={"type": "object", "properties": {}},
            ),
        )
    )
    try:
        with pytest.raises(PermissionError, match="provider tools"):
            await captured[0].open(
                provider.name,
                setup,
                provider_session_id="provider-attachment-1",
            )
        assert provider.opened == 0
    finally:
        _reset_for_tests()


def test_attachment_admits_pcm_only_for_exact_native_operator_speaker():
    from gateway.realtime_voice_controller import AudioFeedResult
    from gateway.realtime_voice_messaging_host import GatewayRealtimeVoiceAttachment

    controller = MagicMock()
    controller.feed_audio.return_value = AudioFeedResult.ACCEPTED
    attachment = GatewayRealtimeVoiceAttachment(
        _binding("discord:guild-1:channel-1:thread-1"),
        controller,
        operator_user_id="123456789",
    )

    assert attachment.operator_user_id == 123456789

    for lookalike in (True, "123456789", 123456789.0, 987654321):
        assert (
            attachment.feed_audio(b"pcm", speaker_user_id=lookalike)
            is AudioFeedResult.UNAUTHORIZED
        )
    controller.feed_audio.assert_not_called()

    assert (
        attachment.feed_audio(b"pcm", speaker_user_id=123456789)
        is AudioFeedResult.ACCEPTED
    )
    controller.feed_audio.assert_called_once_with(b"pcm", mime_type=None)

    controller.feed_audio.reset_mock()
    assert (
        attachment.feed_synthesized_silence(b"\x00\x00\x00\x00")
        is AudioFeedResult.ACCEPTED
    )
    controller.feed_audio.assert_called_once_with(
        b"\x00\x00\x00\x00", mime_type="audio/pcm"
    )
    for malformed in (b"", b"\x00", b"\x00\x01", bytearray(b"\x00\x00")):
        assert (
            attachment.feed_synthesized_silence(malformed)
            is AudioFeedResult.UNAUTHORIZED
        )


def test_session_db_marker_stamps_only_exact_owned_active_row(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("session-a", "gateway")
        db.create_session("session-b", "gateway")
        row_a = db.append_message("session-a", "user", "same text")
        row_b = db.append_message("session-b", "user", "same text")

        assert not db.set_message_display_kind(
            "session-a",
            row_b,
            display_kind="realtime_voice_turn",
            display_metadata={"realtime_voice_turn_marker": "wrong-owner"},
        )
        assert db.set_message_display_kind(
            "session-a",
            row_a,
            display_kind="realtime_voice_turn",
            display_metadata={"realtime_voice_turn_marker": "exact-row"},
        )

        messages_a = db.get_messages("session-a", include_inactive=True)
        messages_b = db.get_messages("session-b", include_inactive=True)
        assert messages_a[0]["display_kind"] == "realtime_voice_turn"
        assert messages_a[0]["display_metadata"] == {
            "realtime_voice_turn_marker": "exact-row"
        }
        assert messages_b[0]["display_kind"] is None
    finally:
        db.close()
