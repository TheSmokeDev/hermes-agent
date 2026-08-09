from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from agent.realtime_voice_orchestrator import (
    RealtimeVoiceOrchestrator,
    open_realtime_voice_session,
)
from agent.realtime_voice_provider import (
    RealtimeCapability,
    RealtimeToolResult,
    RealtimeVoiceEvent,
    RealtimeVoiceProvider,
    RealtimeVoiceSession,
    RealtimeVoiceSetup,
    SessionClosed,
    SessionReady,
    UnsupportedRealtimeCapability,
)
from agent.realtime_voice_registry import _reset_for_tests, register_provider


class _Session(RealtimeVoiceSession):
    def __init__(self, events: tuple[RealtimeVoiceEvent, ...]) -> None:
        super().__init__()
        self._event_values = events
        self.close_calls = 0

    async def send_audio(
        self, audio: bytes, *, mime_type: str | None = None
    ) -> None:
        pass

    async def _submit_tool_results(
        self, batch_id: str, results: tuple[RealtimeToolResult, ...]
    ) -> None:
        pass

    async def _events(self) -> AsyncIterator[RealtimeVoiceEvent]:
        for event in self._event_values:
            yield event

    async def _close(self) -> None:
        self.close_calls += 1


class _Provider(RealtimeVoiceProvider):
    def __init__(self, session: _Session, *, available: bool = True) -> None:
        self._session = session
        self._available = available
        self.opened_setups: list[RealtimeVoiceSetup] = []

    @property
    def name(self) -> str:
        return "fake"

    def is_available(self) -> bool:
        return self._available

    async def open_session(self, setup: RealtimeVoiceSetup) -> RealtimeVoiceSession:
        self.opened_setups.append(setup)
        return self._session


class _Host:
    def __init__(self) -> None:
        self.events: list[RealtimeVoiceEvent] = []

    async def handle_realtime_event(self, event: RealtimeVoiceEvent) -> None:
        self.events.append(event)


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    _reset_for_tests()
    yield
    _reset_for_tests()


@pytest.mark.asyncio
async def test_registered_provider_events_reach_host_and_session_closes() -> None:
    events = (SessionReady(session_id="session"), SessionClosed(reason="done"))
    session = _Session(events)
    provider = _Provider(session)
    host = _Host()
    setup = RealtimeVoiceSetup(model="model")
    assert register_provider(provider)

    await RealtimeVoiceOrchestrator(host).run("fake", setup)

    assert provider.opened_setups == [setup]
    assert host.events == list(events)
    assert session.close_calls == 1


@pytest.mark.asyncio
async def test_missing_required_capability_fails_before_session_open() -> None:
    session = _Session(())
    provider = _Provider(session)
    assert register_provider(provider)

    with pytest.raises(UnsupportedRealtimeCapability) as exc_info:
        await RealtimeVoiceOrchestrator(_Host()).run(
            "fake",
            RealtimeVoiceSetup(),
            required_capabilities=frozenset({RealtimeCapability.TOOL_CALLING}),
        )

    assert exc_info.value.capability is RealtimeCapability.TOOL_CALLING
    assert provider.opened_setups == []


@pytest.mark.asyncio
async def test_shared_open_rejects_unavailable_provider_before_open() -> None:
    session = _Session(())
    provider = _Provider(session, available=False)
    assert register_provider(provider)

    with pytest.raises(RuntimeError, match="unavailable"):
        await open_realtime_voice_session("fake", RealtimeVoiceSetup())

    assert provider.opened_setups == []
