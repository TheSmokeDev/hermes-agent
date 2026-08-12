from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections.abc import AsyncIterator
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.realtime_voice_admission import RealtimeSessionBinding, RealtimeUtterance
from agent.realtime_voice_provider import (
    InputTranscript,
    OutputAudio,
    OutputTranscript,
    RealtimeCapability,
    RealtimeOutputAudioFormat,
    RealtimeResponseRequest,
    RealtimeToolResult,
    RealtimeVoiceEvent,
    RealtimeVoiceProvider,
    RealtimeVoiceSession,
    RealtimeVoiceSetup,
    ResponseCompleted,
    ResponseStarted,
    SessionReady,
    ToolCall,
    TranscriptProvenance,
    TranscriptRole,
)
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource, build_session_key


def _discord_source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="222222222222222222",
        chat_type="group",
        user_id="111111111111111111",
        thread_id="333333333333333333",
        scope_id="444444444444444444",
        is_bot=False,
    )


async def _capture_factory(runner, source: SessionSource, route: str):
    from gateway.realtime_voice_invocation import _invoke_plugin_command_with_context
    from gateway.config import PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter

    if type(runner.adapters.get(Platform.DISCORD)) is not DiscordAdapter:
        adapter = DiscordAdapter(PlatformConfig(enabled=True))
        adapter.gateway_runner = runner
        adapter.set_message_handler(lambda event: runner._handle_message(event))
        runner.adapters[Platform.DISCORD] = adapter

    captured = []
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
    return captured[0]


class _InstalledSession(RealtimeVoiceSession):
    def __init__(self) -> None:
        super().__init__(frozenset({
            RealtimeCapability.EXPLICIT_RESPONSE,
            RealtimeCapability.RESPONSE_CANCELLATION,
        }))
        self.incoming: asyncio.Queue[RealtimeVoiceEvent] = asyncio.Queue()
        self.close_calls = 0
        self.tool_result_calls = 0
        self.response_requests: list[RealtimeResponseRequest] = []
        self.response_requested = asyncio.Event()

    async def send_audio(self, audio: bytes, *, mime_type: str | None = None) -> None:
        pass

    async def _submit_tool_results(
        self, batch_id: str, results: tuple[RealtimeToolResult, ...]
    ) -> None:
        self.tool_result_calls += 1
        raise AssertionError("provider tools must remain inert")

    async def _start_response(self, request: RealtimeResponseRequest) -> None:
        self.response_requests.append(request)
        self.response_requested.set()

    async def _cancel_response(self, response_id: str) -> None:
        raise AssertionError("completed native response must not be cancelled")

    async def _events(self) -> AsyncIterator[RealtimeVoiceEvent]:
        while True:
            yield await self.incoming.get()

    async def _close(self) -> None:
        self.close_calls += 1


class _InstalledProvider(RealtimeVoiceProvider):
    capabilities = frozenset({
        RealtimeCapability.EXPLICIT_RESPONSE,
        RealtimeCapability.RESPONSE_CANCELLATION,
    })

    def __init__(self, session: _InstalledSession) -> None:
        self.session = session
        self.open_calls = 0

    @property
    def name(self) -> str:
        return "installed-messaging-canary"

    async def open_session(self, setup: RealtimeVoiceSetup) -> RealtimeVoiceSession:
        self.open_calls += 1
        return self.session


@pytest.mark.asyncio
async def test_native_realtime_voice_turn_bypasses_generic_whole_file_auto_tts(
    monkeypatch, tmp_path
):
    """The exact host-owned turn reaches canonical delivery without generic TTS."""
    import gateway.run as gateway_run
    import hermes_state
    import run_agent
    from agent.realtime_voice_registry import _reset_for_tests, register_provider
    from gateway.realtime_voice_controller import (
        ControllerLifecycle,
        GatewayRealtimeVoiceController,
    )
    from gateway.config import PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.realtime_voice_messaging_host import (
        _ACCEPTED_TASKS,
        _CLAIM_ATTR,
        _MARKER_KEY,
        GatewayRealtimeVoiceMessagingHost,
        RealtimeVoiceFinalizationReceipt,
    )
    from gateway.session import AsyncSessionStore, SessionStore
    from gateway.turn_lease import SessionTurnLeaseRegistry
    from hermes_state import AsyncSessionDB, SessionDB
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
    from plugins.platforms.discord.adapter import DiscordAdapter
    from tests.gateway.test_42039_duplicate_user_message import _bootstrap

    _reset_for_tests()
    runner = _bootstrap(monkeypatch, tmp_path)
    # The broad bootstrap constructs production objects before narrowing them
    # for its legacy tests. Close those handles, then install one real DB shared
    # by the real SessionStore and runner exactly as production does.
    closed: set[int] = set()
    for candidate in (
        getattr(runner.session_store, "_db", None),
        getattr(getattr(runner, "_session_db", None), "_db", None),
    ):
        if candidate is not None and id(candidate) not in closed:
            candidate.close()
            closed.add(id(candidate))

    db = SessionDB(db_path=tmp_path / "installed-state.db")
    real_session_db_type = hermes_state.SessionDB
    monkeypatch.setattr(hermes_state, "SessionDB", lambda: db)
    store = SessionStore(tmp_path / "sessions", runner.config)
    monkeypatch.setattr(hermes_state, "SessionDB", real_session_db_type)

    source = _discord_source()
    route = build_session_key(source)
    now = datetime.now()
    entry = SessionEntry(
        session_key=route,
        session_id="durable-installed-realtime",
        created_at=now,
        updated_at=now,
        platform=Platform.DISCORD,
        chat_type="group",
    )
    db.create_session(session_id=entry.session_id, source="discord")
    store._entries[route] = entry
    store._loaded = True
    store._route_structural_generations[route] = 1
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._session_db = AsyncSessionDB(db)
    runner._turn_leases = SessionTurnLeaseRegistry()
    runner._turn_lease_tokens = {}
    runner._external_drain_active = False
    runner._persist_active_agents = MagicMock()

    adapter = DiscordAdapter(PlatformConfig(enabled=True, typing_indicator=False))
    adapter.gateway_runner = runner
    adapter.set_message_handler(runner._handle_message)
    adapter.set_session_store(store)
    runner.adapters[Platform.DISCORD] = adapter
    adapter._should_auto_tts_for_chat = lambda _chat_id: True
    canonical_sends: list[tuple[str, str]] = []
    generic_tts_calls: list[str] = []
    generic_audio_calls: list[str] = []
    generic_voice_calls: list[str] = []

    def generic_tts_tripwire(*, text, output_path=None):
        generic_tts_calls.append(text)
        assert output_path is not None
        from pathlib import Path

        Path(output_path).write_bytes(b"generic whole-file audio")
        return json.dumps({"success": True, "file_path": output_path})

    async def generic_audio_tripwire(*, audio_path, **_kwargs):
        generic_audio_calls.append(audio_path)
        raise AssertionError("native realtime turn reached generic audio output")

    async def generic_voice_tripwire(*, audio_path, **_kwargs):
        generic_voice_calls.append(audio_path)
        raise AssertionError("native realtime turn reached voice attachment fallback")

    monkeypatch.setattr("tools.tts_tool.check_tts_requirements", lambda: True)
    monkeypatch.setattr("tools.tts_tool.text_to_speech_tool", generic_tts_tripwire)
    monkeypatch.setattr(adapter, "play_tts", generic_audio_tripwire)
    monkeypatch.setattr(adapter, "send_voice", generic_voice_tripwire)

    class _NativeLease:
        def __init__(self) -> None:
            self.pcm: list[bytes] = []
            self.finish_entered = asyncio.Event()
            self.release_finish = asyncio.Event()
            self.close_calls = 0

        async def write_pcm(self, data: bytes) -> None:
            self.pcm.append(data)

        async def finish_and_wait(self, _timeout: float):
            self.finish_entered.set()
            await self.release_finish.wait()
            return drain_receipt

        async def interrupt_and_wait(self, _timeout: float):
            raise AssertionError("completed native playback must drain")

        async def close(self) -> None:
            self.close_calls += 1

    class _DrainReceipt:
        interrupted = False

    drain_receipt = _DrainReceipt()
    native_lease = _NativeLease()
    receipt_validated = asyncio.Event()
    guild_id = int(source.scope_id)
    adapter._voice_clients[guild_id] = type(
        "ConnectedVoice", (), {"is_connected": lambda self: True}
    )()
    adapter._voice_connection_generations[guild_id] = 1
    adapter._voice_mixer_generations[guild_id] = 1
    lease_acquired = asyncio.Event()

    async def acquire_native_lease(*_args, **_kwargs):
        lease_acquired.set()
        return native_lease

    monkeypatch.setattr(
        adapter,
        "acquire_native_playback_lease",
        AsyncMock(side_effect=acquire_native_lease),
    )

    def validate_native_receipt(candidate_guild, candidate_lease, candidate_receipt):
        assert candidate_guild == guild_id
        assert candidate_lease is native_lease
        assert candidate_receipt is drain_receipt
        receipt_validated.set()
        return candidate_receipt

    monkeypatch.setattr(
        adapter, "validate_native_playback_receipt", validate_native_receipt
    )

    async def send_canonical_response(*, chat_id, content, **_kwargs):
        canonical_sends.append((chat_id, content))
        return SendResult(success=True, message_id="canonical-delivery")

    monkeypatch.setattr(adapter, "_send_with_retry", send_canonical_response)

    captured_events: list[MessageEvent] = []

    def capture_pre_dispatch(_hook_name, **kwargs):
        event = kwargs["event"]
        if getattr(event, _CLAIM_ATTR, None) is not None:
            event.message_type = MessageType.VOICE
        captured_events.append(event)
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", capture_pre_dispatch)

    handler_started = asyncio.Event()
    release_handler = asyncio.Event()
    run_calls: list[str] = []

    async def controlled_model_io(**kwargs):
        message = kwargs["message"]
        assert kwargs["session_id"] == entry.session_id
        assert kwargs["session_key"] == route
        assert kwargs["force_nonstream"] is True
        run_calls.append(message)
        handler_started.set()
        await release_handler.wait()
        return {
            "final_response": "canonical installed response",
            "messages": [
                {"role": "user", "content": message},
                {"role": "assistant", "content": "canonical installed response"},
            ],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
            "agent_persisted": False,
        }

    # This is the only patched production seam: external model execution. The
    # real _handle_message, SessionStore, turn lease, SessionDB, factory,
    # controller, registry, and provider event pump all remain installed.
    runner._run_agent = controlled_model_io

    constructor_calls = {"controller": 0}
    real_controller_init = GatewayRealtimeVoiceController.__init__

    def counted_controller_init(self, *args, **kwargs):
        constructor_calls["controller"] += 1
        real_controller_init(self, *args, **kwargs)

    monkeypatch.setattr(
        GatewayRealtimeVoiceController, "__init__", counted_controller_init
    )
    monkeypatch.setattr(
        gateway_run.GatewayRunner,
        "__init__",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a second GatewayRunner must not be constructed")
        ),
    )
    monkeypatch.setattr(
        run_agent.AIAgent,
        "__init__",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a second AIAgent/legacy executor must not be constructed")
        ),
    )
    monkeypatch.setattr(
        real_session_db_type,
        "__init__",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a second SessionDB must not be constructed")
        ),
    )
    monkeypatch.setattr(
        SessionStore,
        "__init__",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a second SessionStore must not be constructed")
        ),
    )
    real_host_init = GatewayRealtimeVoiceMessagingHost.__init__
    host_constructions = 0

    def single_host_init(self, *args, **kwargs):
        nonlocal host_constructions
        host_constructions += 1
        if host_constructions > 1:
            raise AssertionError("a second messaging host must not be constructed")
        real_host_init(self, *args, **kwargs)

    monkeypatch.setattr(GatewayRealtimeVoiceMessagingHost, "__init__", single_host_init)

    session = _InstalledSession()
    provider = _InstalledProvider(session)
    assert register_provider(provider)
    manager = get_plugin_manager()
    # Cross-repo follow-up contract: the sibling Talk plugin must register its
    # public ``/talk`` command with ``invocation_context=True`` and exercise
    # ``/talk core join`` through this same adapter dispatch. Agent deliberately
    # does not import or claim execution of sibling Talk code in this test.
    command_name = "agent-realtime-canary"
    previous_registration = manager._plugin_commands.get(command_name)
    captured_factories = []

    def installed_handler(raw_args, invocation=None):
        assert raw_args == "core join"
        assert invocation is not None
        captured_factories.append(
            invocation.capture_realtime_voice_attachment_factory()
        )

    PluginContext(PluginManifest(name="agent-realtime-test"), manager).register_command(
        command_name,
        installed_handler,
        invocation_context=True,
    )
    command_event = MessageEvent(
        text=f"/{command_name} core join",
        message_type=MessageType.TEXT,
        source=source,
        user_id=source.user_id,
        metadata={},
    )
    await adapter.handle_message(command_event)
    command_task = adapter._session_tasks.get(route)
    assert command_task is not None
    await asyncio.wait_for(command_task, timeout=5)
    assert len(captured_factories) == 1
    factory = captured_factories[0]
    attachment = await factory.open(
        provider.name,
        RealtimeVoiceSetup(output_audio=RealtimeOutputAudioFormat(
            mime_type="audio/pcm", sample_rate_hz=24000, channels=1,
            sample_encoding="pcm_s16le", sample_width_bytes=2,
            endianness="little",
        )),
        provider_session_id="provider-installed-session",
    )
    tool = ToolCall(
        call_id="provider-call",
        batch_id="provider-batch",
        turn_id="provider-tool-turn",
        response_id="provider-response",
        name="must_not_execute",
        arguments={},
    )
    final = InputTranscript(
        item_id="provider-final-item",
        turn_id="provider-final-turn",
        text="installed voice turn",
        final=True,
        role=TranscriptRole.OPERATOR,
        provenance=TranscriptProvenance.OPERATOR_INPUT,
    )

    try:
        await session.incoming.put(SessionReady(session_id="provider-native"))
        await session.incoming.put(tool)
        await session.incoming.put(final)
        await asyncio.wait_for(handler_started.wait(), timeout=5)

        release_handler.set()
        accepted = tuple(_ACCEPTED_TASKS)
        assert len(accepted) == 1
        results = await asyncio.wait_for(asyncio.gather(*accepted), timeout=5)
        assert len(results) == 1
        await asyncio.wait_for(session.response_requested.wait(), timeout=5)
        assert len(session.response_requests) == 1
        request = session.response_requests[0]
        assert request == RealtimeResponseRequest(
            durable_session_id=entry.session_id,
            assistant_message_id=request.assistant_message_id,
            turn_marker=request.turn_marker,
            canonical_text="canonical installed response",
            content_digest=hashlib.sha256(
                b"canonical installed response"
            ).hexdigest(),
            output_audio_format=RealtimeOutputAudioFormat(
                mime_type="audio/pcm",
                sample_rate_hz=24000,
                channels=1,
                sample_encoding="pcm_s16le",
                sample_width_bytes=2,
                endianness="little",
            ),
            allow_tools=False,
        )
        response_id = "provider-native-response"
        item_id = "provider-output-item"
        controller = attachment._controller
        native_state = controller._native_response
        assert native_state is not None

        class _StartedSubclass(ResponseStarted):
            pass

        invalid_before_start = (
            ResponseStarted(response_id, "wrong-turn-marker"),
            OutputAudio(b"\x00\x00", item_id, request.turn_marker, response_id),
            _StartedSubclass(response_id, request.turn_marker),
        )
        for invalid in invalid_before_start:
            with pytest.raises(RuntimeError):
                await controller._handle_native_event(invalid, 1)
            assert native_state.response_id is None
            adapter.acquire_native_playback_lease.assert_not_awaited()

        await controller._handle_native_event(
            ResponseStarted(response_id, request.turn_marker), 1
        )
        assert lease_acquired.is_set()
        adapter.acquire_native_playback_lease.assert_awaited_once_with(
            guild_id,
            native_state.lease_id,
            response_id,
            request.turn_marker,
            1,
            1,
            input_format="pcm_s16le",
            sample_rate=24000,
            channels=1,
        )
        with pytest.raises(RuntimeError, match="duplicate native response start"):
            await controller._handle_native_event(
                ResponseStarted(response_id, request.turn_marker), 1
            )
        with pytest.raises(RuntimeError, match="native response identity mismatch"):
            await controller._handle_native_event(
                OutputAudio(b"\x00\x00", item_id, "wrong-turn", response_id), 1
            )
        mutated = OutputAudio(
            b"\x00\x00", item_id, request.turn_marker, response_id
        )
        object.__setattr__(mutated, "response_id", "mutated-response")
        with pytest.raises(RuntimeError, match="native response identity mismatch"):
            await controller._handle_native_event(mutated, 1)
        assert native_lease.pcm == []
        await session.incoming.put(
            OutputAudio(b"\x00\x00", item_id, request.turn_marker, response_id)
        )
        await session.incoming.put(
            OutputTranscript(
                item_id,
                request.turn_marker,
                response_id,
                "provider projection must not replace canonical text",
                True,
                TranscriptRole.ASSISTANT,
                TranscriptProvenance.ASSISTANT_OUTPUT_AUDIO,
            )
        )
        await session.incoming.put(
            OutputAudio(b"\x01\x00", item_id, request.turn_marker, response_id)
        )
        await session.incoming.put(ResponseCompleted(response_id, request.turn_marker))
        await asyncio.wait_for(native_lease.finish_entered.wait(), timeout=5)
        assert controller._native_response is not None
        assert not receipt_validated.is_set()
        assert not any(
            event.lifecycle is ControllerLifecycle.COMPLETED
            for event in controller.lifecycle_events
        )
        native_lease.release_finish.set()
        await asyncio.wait_for(receipt_validated.wait(), timeout=5)
        terminal_task = controller._native_terminal_task
        assert terminal_task is not None
        await asyncio.wait_for(asyncio.shield(terminal_task), timeout=5)
        assert controller._native_response is None
        with pytest.raises(RuntimeError, match="late duplicate native response event"):
            await controller._handle_native_event(
                OutputAudio(b"\x02\x00", item_id, request.turn_marker, response_id), 1
            )
        assert native_lease.pcm == [b"\x00\x00", b"\x01\x00"]
        await asyncio.wait_for(attachment.close(), timeout=5)

        rows = db.get_messages(entry.session_id, include_inactive=True)
        user_rows = [row for row in rows if row["role"] == "user"]
        assistant_rows = [row for row in rows if row["role"] == "assistant"]
        assert [row["content"] for row in user_rows] == ["installed voice turn"]
        assert [row["content"] for row in assistant_rows] == [
            "canonical installed response"
        ]
        user_row = user_rows[0]
        assistant_row = assistant_rows[0]
        assert user_row["id"] < assistant_row["id"]
        assert user_row["display_kind"] == "realtime_voice_turn"
        marker = user_row["display_metadata"][_MARKER_KEY]
        assert type(marker) is str and len(marker) == 32

        claim = getattr(captured_events[-1], _CLAIM_ATTR)
        receipt = claim.receipt
        assert type(receipt) is RealtimeVoiceFinalizationReceipt
        assert receipt.turn_marker == marker
        assert receipt.user_message_id == user_row["id"]
        assert receipt.assistant_message_id == assistant_row["id"]
        assert not claim.host.validate_finalization(receipt)
        reread = db.get_messages(entry.session_id, include_inactive=True)
        reread_user = next(row for row in reread if row["id"] == user_row["id"])
        assert reread_user["display_kind"] == "realtime_voice_turn"
        assert reread_user["display_metadata"][_MARKER_KEY] == marker

        assert run_calls == ["installed voice turn"]
        assert request.assistant_message_id == assistant_row["id"]
        assert request.turn_marker == marker
        assert [sent.canonical_text for sent in session.response_requests] == [
            "canonical installed response"
        ]
        assert native_lease.pcm == [b"\x00\x00", b"\x01\x00"]
        assert canonical_sends == [(source.chat_id, "canonical installed response")]
        assert generic_tts_calls == []
        assert generic_audio_calls == []
        assert generic_voice_calls == []
        assert provider.open_calls == 1
        assert session.tool_result_calls == 0
        assert session.close_calls == 1
        assert constructor_calls == {"controller": 1}
        assert host_constructions == 1
        assert not runner._is_session_running(route)
        assert runner._turn_lease_tokens == {}
        assert not _ACCEPTED_TASKS
        completed = [
            event
            for event in controller.lifecycle_events
            if event.lifecycle is ControllerLifecycle.COMPLETED
        ]
        assert len(completed) == 1
        assert completed[0].detail == "native playback drained"
        assert receipt_validated.is_set()
        assert native_lease.close_calls == 0
        host = claim.host
        assert host._reservations == set()
        assert host._acquisitions == {}
        assert len(host._requests) == 0
        assert controller._event_task.done()
        assert controller._audio_task.done()
        assert controller._closed is True
    finally:
        if previous_registration is None:
            manager._plugin_commands.pop(command_name, None)
        else:
            manager._plugin_commands[command_name] = previous_registration
        release_handler.set()
        await attachment.close()
        db.close()
        _reset_for_tests()


@pytest.mark.asyncio
async def test_attached_nonstream_send_failure_has_no_completion_receipt():
    from gateway.config import PlatformConfig
    from gateway.platforms.base import SendResult
    from plugins.platforms.discord.adapter import DiscordAdapter

    source = _discord_source()
    route = build_session_key(source)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, typing_indicator=False))
    runner = MagicMock()
    runner._adapter_for_source.return_value = adapter
    adapter.gateway_runner = runner
    handler_calls = 0
    send_calls = 0

    async def handler(_event):
        nonlocal handler_calls
        handler_calls += 1
        return "persisted but undeliverable response"

    async def failed_send(**_kwargs):
        nonlocal send_calls
        send_calls += 1
        return SendResult(success=False, error="discord denied delivery")

    adapter.set_message_handler(handler)
    adapter._send_with_retry = failed_send
    event = MessageEvent(
        text="voice turn",
        message_type=MessageType.TEXT,
        source=source,
        user_id=source.user_id,
        metadata={},
    )

    with pytest.raises(RuntimeError, match="canonical response delivery failed"):
        await adapter._process_attached_message(event, route)

    assert handler_calls == 1
    assert send_calls == 1
    assert route not in adapter._active_sessions
    assert route not in adapter._session_tasks


@pytest.mark.asyncio
async def test_attached_media_only_success_has_completion_receipt(tmp_path):
    from gateway.config import PlatformConfig
    from gateway.platforms.base import SendResult
    from plugins.platforms.discord.adapter import DiscordAdapter

    document = tmp_path / "voice-result.pdf"
    document.write_bytes(b"result")
    source = _discord_source()
    route = build_session_key(source)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, typing_indicator=False))
    runner = MagicMock()
    runner._adapter_for_source.return_value = adapter
    adapter.gateway_runner = runner
    adapter.set_message_handler(
        lambda _event: asyncio.sleep(0, result=f"MEDIA:{document}")
    )
    adapter._send_with_retry = AsyncMock(
        side_effect=AssertionError("media-only response must not send duplicate text")
    )
    adapter.send_document = AsyncMock(
        return_value=SendResult(success=True, message_id="document-delivered")
    )
    event = MessageEvent(
        text="voice turn",
        message_type=MessageType.TEXT,
        source=source,
        user_id=source.user_id,
        metadata={},
    )

    completed = await adapter._process_attached_message(event, route)

    assert completed is True
    adapter.send_document.assert_awaited_once()
    adapter._send_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_attached_text_and_failed_attachment_has_no_completion_receipt(tmp_path):
    from gateway.config import PlatformConfig
    from gateway.platforms.base import SendResult
    from plugins.platforms.discord.adapter import DiscordAdapter

    document = tmp_path / "required-result.pdf"
    document.write_bytes(b"result")
    source = _discord_source()
    route = build_session_key(source)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, typing_indicator=False))
    runner = MagicMock()
    runner._adapter_for_source.return_value = adapter
    adapter.gateway_runner = runner
    adapter.set_message_handler(
        lambda _event: asyncio.sleep(0, result=f"answer\nMEDIA:{document}")
    )
    adapter._send_with_retry = AsyncMock(
        return_value=SendResult(success=True, message_id="text-delivered")
    )
    adapter.send_document = AsyncMock(
        return_value=SendResult(success=False, error="attachment rejected")
    )
    event = MessageEvent(
        text="voice turn",
        message_type=MessageType.TEXT,
        source=source,
        user_id=source.user_id,
        metadata={},
    )

    with pytest.raises(RuntimeError, match="canonical response delivery failed"):
        await adapter._process_attached_message(event, route)

    adapter._send_with_retry.assert_awaited_once()
    adapter.send_document.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("suffix", "response_template"),
    [
        (".png", "MEDIA:{path}"),
        (".mp4", "MEDIA:{path}"),
        (".pdf", "MEDIA:{path}"),
        (".txt", "{path}"),
    ],
    ids=["image", "video", "document", "local-file"],
)
async def test_attached_media_revalidates_exact_route_before_each_send(
    tmp_path, suffix, response_template
):
    from gateway.config import PlatformConfig
    from gateway.platforms.base import SendResult
    from plugins.platforms.discord.adapter import DiscordAdapter

    artifact = tmp_path / f"result{suffix}"
    artifact.write_bytes(b"result")
    source = _discord_source()
    route = build_session_key(source)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, typing_indicator=False))
    replacement = DiscordAdapter(PlatformConfig(enabled=True, typing_indicator=False))
    runner = MagicMock()
    runner._adapter_for_source.return_value = adapter
    adapter.gateway_runner = runner

    async def handler(_event):
        runner._adapter_for_source.return_value = replacement
        return response_template.format(path=artifact)

    adapter.set_message_handler(handler)
    adapter.send_multiple_images = AsyncMock(
        return_value=SendResult(success=True, message_id="stale-image")
    )
    adapter.send_video = AsyncMock(
        return_value=SendResult(success=True, message_id="stale-video")
    )
    adapter.send_document = AsyncMock(
        return_value=SendResult(success=True, message_id="stale-document")
    )
    adapter._send_with_retry = AsyncMock(
        return_value=SendResult(success=True, message_id="stale-text")
    )
    event = MessageEvent(
        text="voice turn",
        message_type=MessageType.TEXT,
        source=source,
        user_id=source.user_id,
        metadata={},
    )

    with pytest.raises(
        RuntimeError, match="adapter changed before (?:final delivery|delivery proof)"
    ):
        await adapter._process_attached_message(event, route)

    adapter.send_multiple_images.assert_not_awaited()
    adapter.send_video.assert_not_awaited()
    adapter.send_document.assert_not_awaited()
    adapter._send_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_attached_handler_cannot_import_or_forge_delivery_completion():
    import gateway.platforms.base as base
    from gateway.config import PlatformConfig
    from gateway.platforms.base import SendResult
    from plugins.platforms.discord.adapter import DiscordAdapter

    for removed_name in (
        "_ATTACHED_MESSAGE_MINT",
        "_ATTACHED_MESSAGE_TRACKER_ATTR",
        "_AttachedMessageCompletionReceipt",
        "_AttachedMessageDeliveryTracker",
        "_record_attached_message_delivery",
    ):
        assert not hasattr(base, removed_name)

    source = _discord_source()
    route = build_session_key(source)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, typing_indicator=False))
    runner = MagicMock()
    runner._adapter_for_source.return_value = adapter
    adapter.gateway_runner = runner

    async def forged_handler(event):
        event.metadata["delivery_result"] = SendResult(
            success=True, message_id="caller-forged"
        )
        return None

    adapter.set_message_handler(forged_handler)
    event = MessageEvent(
        text="voice turn",
        message_type=MessageType.TEXT,
        source=source,
        user_id=source.user_id,
        metadata={},
    )

    with pytest.raises(RuntimeError, match="canonical response delivery failed"):
        await adapter._process_attached_message(event, route)


@pytest.mark.asyncio
async def test_attached_handler_cannot_replace_send_method_to_forge_completion():
    from gateway.config import PlatformConfig
    from gateway.platforms.base import SendResult
    from plugins.platforms.discord.adapter import DiscordAdapter

    source = _discord_source()
    route = build_session_key(source)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, typing_indicator=False))
    runner = MagicMock()
    runner._adapter_for_source.return_value = adapter
    adapter.gateway_runner = runner
    trusted_send = AsyncMock(
        return_value=SendResult(success=False, error="physical send unavailable")
    )
    forged_send = AsyncMock(
        return_value=SendResult(success=True, message_id="forged-without-io")
    )
    adapter._send_with_retry = trusted_send

    async def forged_handler(_event):
        adapter._send_with_retry = forged_send
        return "canonical response"

    adapter.set_message_handler(forged_handler)
    event = MessageEvent(
        text="voice turn",
        message_type=MessageType.TEXT,
        source=source,
        user_id=source.user_id,
        metadata={},
    )

    with pytest.raises(RuntimeError, match="attached delivery method changed"):
        await adapter._process_attached_message(event, route)

    trusted_send.assert_not_awaited()
    forged_send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("suffix", "response_template"),
    [(".pdf", "MEDIA:{path}"), (".txt", "{path}")],
    ids=["media", "local-file"],
)
async def test_attached_failed_send_after_route_replacement_emits_no_stale_notice(
    tmp_path, suffix, response_template
):
    from gateway.config import PlatformConfig
    from gateway.platforms.base import SendResult
    from plugins.platforms.discord.adapter import DiscordAdapter

    artifact = tmp_path / f"required{suffix}"
    artifact.write_bytes(b"result")
    source = _discord_source()
    route = build_session_key(source)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, typing_indicator=False))
    replacement = DiscordAdapter(PlatformConfig(enabled=True, typing_indicator=False))
    runner = MagicMock()
    runner._adapter_for_source.return_value = adapter
    adapter.gateway_runner = runner
    adapter.set_message_handler(
        lambda _event: asyncio.sleep(
            0, result=response_template.format(path=artifact)
        )
    )

    async def failed_send(**_kwargs):
        runner._adapter_for_source.return_value = replacement
        return SendResult(success=False, error="upload rejected")

    adapter.send_document = AsyncMock(side_effect=failed_send)
    adapter._notify_media_delivery_failure = AsyncMock()
    event = MessageEvent(
        text="voice turn",
        message_type=MessageType.TEXT,
        source=source,
        user_id=source.user_id,
        metadata={},
    )

    with pytest.raises(RuntimeError, match="canonical response delivery failed"):
        await adapter._process_attached_message(event, route)

    adapter.send_document.assert_awaited_once()
    adapter._notify_media_delivery_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_attached_nonstream_response_is_sent_once_by_canonical_delivery():
    from gateway.config import PlatformConfig
    from gateway.platforms.base import SendResult
    from plugins.platforms.discord.adapter import DiscordAdapter

    source = _discord_source()
    route = build_session_key(source)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, typing_indicator=False))
    runner = MagicMock()
    runner._adapter_for_source.return_value = adapter
    adapter.gateway_runner = runner
    adapter._send_with_retry = AsyncMock(
        return_value=SendResult(success=True, message_id="canonical-final")
    )
    adapter.set_message_handler(
        lambda _event: asyncio.sleep(0, result="one canonical response")
    )
    event = MessageEvent(
        text="voice turn",
        message_type=MessageType.TEXT,
        source=source,
        user_id=source.user_id,
        metadata={},
    )

    completed = await adapter._process_attached_message(event, route)

    assert completed is True
    adapter._send_with_retry.assert_awaited_once()
    assert (
        adapter._send_with_retry.await_args.kwargs["content"]
        == "one canonical response"
    )


@pytest.mark.asyncio
async def test_attached_handoff_keeps_guard_through_queued_drain_and_third_message():
    from gateway.config import PlatformConfig
    from gateway.platforms.base import SendResult
    from plugins.platforms.discord.adapter import DiscordAdapter

    source = _discord_source()
    route = build_session_key(source)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, typing_indicator=False))
    runner = MagicMock()
    runner._adapter_for_source.return_value = adapter
    adapter.gateway_runner = runner
    adapter._busy_text_mode = "queue"
    adapter._busy_text_debounce_seconds = 0.0
    adapter._send_with_retry = AsyncMock(
        return_value=SendResult(success=True, message_id="delivered")
    )

    started = {name: asyncio.Event() for name in ("attached", "queued", "third")}
    release = {name: asyncio.Event() for name in ("attached", "queued", "third")}
    active = 0
    max_active = 0
    calls: list[str] = []

    async def handler(event):
        nonlocal active, max_active
        name = event.text
        calls.append(name)
        active += 1
        max_active = max(max_active, active)
        started[name].set()
        try:
            await release[name].wait()
            return f"response:{name}"
        finally:
            active -= 1

    adapter.set_message_handler(handler)

    def make_event(text):
        return MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            user_id=source.user_id,
            metadata={},
        )

    attached_task = asyncio.create_task(
        adapter._process_attached_message(make_event("attached"), route)
    )
    await asyncio.wait_for(started["attached"].wait(), timeout=2)

    await adapter.handle_message(make_event("queued"))
    await asyncio.sleep(0)
    release["attached"].set()
    assert await asyncio.wait_for(attached_task, timeout=2) is True
    await asyncio.wait_for(started["queued"].wait(), timeout=2)

    drain_owner = adapter._session_tasks.get(route)
    assert drain_owner is not None and drain_owner is not attached_task
    assert route in adapter._active_sessions

    await adapter.handle_message(make_event("third"))
    await asyncio.sleep(0)
    assert not started["third"].is_set()
    assert max_active == 1

    release["queued"].set()
    await asyncio.wait_for(started["third"].wait(), timeout=2)
    assert max_active == 1
    release["third"].set()

    for _ in range(100):
        if route not in adapter._session_tasks:
            break
        await asyncio.sleep(0.01)
    assert route not in adapter._session_tasks
    assert route not in adapter._active_sessions
    assert calls == ["attached", "queued", "third"]
    assert max_active == 1


@pytest.mark.asyncio
async def test_realtime_idle_claim_is_linearized_at_actual_running_slot(
    monkeypatch, tmp_path
):
    """A raced ordinary claim wins without any realtime slot mutation."""
    from gateway.realtime_voice_messaging_host import (
        RealtimeVoiceIngressError,
        _create_messaging_host,
    )
    from gateway.turn_lease import SessionTurnLeaseRegistry
    from tests.gateway.test_42039_duplicate_user_message import _bootstrap

    runner = _bootstrap(monkeypatch, tmp_path)
    source = _discord_source()
    route = build_session_key(source)
    entry = SessionEntry(
        session_key=route,
        session_id="durable-realtime-race",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.DISCORD,
        chat_type="group",
    )
    runner.session_store.get_exact_session_entry_snapshot.return_value = (entry, 7)
    runner.session_store.get_or_create_session.return_value = entry
    runner._turn_leases = SessionTurnLeaseRegistry()
    runner._turn_lease_tokens = {}
    runner._external_drain_active = False
    runner._persist_active_agents = MagicMock()
    run_generations = 0

    def begin_run_generation(_route: str) -> int:
        nonlocal run_generations
        run_generations += 1
        return run_generations

    runner._begin_session_run_generation = begin_run_generation

    factory = await _capture_factory(runner, source, route)
    host = _create_messaging_host(factory, runner)
    binding = RealtimeSessionBinding(
        profile_id="default",
        routing_key=route,
        runtime_session_id=route,
        durable_session_id=entry.session_id,
        provider_session_id="provider-race",
        selection_generation=7,
    )
    utterance = RealtimeUtterance(
        provider_session_id="provider-race",
        provider_turn_id="turn-race",
        item_id="item-race",
        text="voice loses the raced slot",
        received_at=1.0,
    )
    permit = await host.authorize(binding, utterance)
    assert permit is not None

    realtime_at_gate = threading.Event()
    release_realtime_gate = threading.Event()
    real_lobby_check = runner._is_telegram_topic_root_lobby

    def gated_lobby_check(candidate: SessionSource) -> bool:
        if candidate is not source:
            realtime_at_gate.set()
            assert release_realtime_gate.wait(timeout=5)
        return real_lobby_check(candidate)

    runner._is_telegram_topic_root_lobby = gated_lobby_check
    ordinary_entered = asyncio.Event()
    release_ordinary = asyncio.Event()
    realtime_entered = asyncio.Event()
    ordinary_agent = object()

    async def gated_handler(event, _source, _route, _generation):
        if event.text == utterance.text:
            realtime_entered.set()
            raise AssertionError("busy realtime work entered the canonical agent path")
        state = runner._session_state(route)
        state.turn.agent = ordinary_agent
        ordinary_entered.set()
        await release_ordinary.wait()
        return {"final_response": "ordinary response"}

    runner._handle_message_with_agent = gated_handler
    realtime_task = asyncio.create_task(host.submit(binding, utterance, permit))
    assert await asyncio.to_thread(realtime_at_gate.wait, 5)

    ordinary_event = MessageEvent(
        text="ordinary turn owns the route",
        message_type=MessageType.TEXT,
        source=source,
        user_id=source.user_id,
        metadata={},
    )
    ordinary_task = asyncio.create_task(runner._handle_message(ordinary_event))
    await asyncio.wait_for(ordinary_entered.wait(), timeout=5)

    state = runner._session_state(route)
    ordinary_started_ts = state.turn.started_ts
    ordinary_generation = run_generations
    ordinary_lease = state.turn.lease
    ordinary_lease_token = state.turn.lease_token
    assert state.turn.agent is ordinary_agent

    release_realtime_gate.set()
    with pytest.raises(RealtimeVoiceIngressError, match="busy"):
        await asyncio.wait_for(realtime_task, timeout=5)

    state = runner._session_state(route)
    assert not realtime_entered.is_set()
    assert state.turn.agent is ordinary_agent
    assert state.turn.started_ts == ordinary_started_ts
    assert run_generations == ordinary_generation
    assert state.turn.lease is ordinary_lease
    assert state.turn.lease_token is ordinary_lease_token
    assert runner._pending_messages == {}

    release_ordinary.set()
    assert await asyncio.wait_for(ordinary_task, timeout=5) == {
        "final_response": "ordinary response"
    }
    assert not runner._is_session_running(route)
    assert runner._turn_lease_tokens == {}
