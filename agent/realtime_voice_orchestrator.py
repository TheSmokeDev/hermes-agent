"""Provider-neutral supervision for one Realtime voice session."""

from __future__ import annotations

from typing import Protocol

from agent.realtime_voice_provider import (
    RealtimeCapability,
    RealtimeVoiceEvent,
    RealtimeVoiceSession,
    RealtimeVoiceSetup,
    UnsupportedRealtimeCapability,
)
from agent.realtime_voice_registry import get_provider


class RealtimeVoiceHost(Protocol):
    """Owning Hermes surface that receives normalized provider events."""

    async def handle_realtime_event(self, event: RealtimeVoiceEvent) -> None:
        """Handle one normalized event without transferring host authority."""


async def open_realtime_voice_session(
    provider_name: str,
    setup: RealtimeVoiceSetup,
    *,
    required_capabilities: frozenset[RealtimeCapability] = frozenset(),
) -> RealtimeVoiceSession:
    """Resolve, validate, and open one registered realtime provider session."""
    provider = get_provider(provider_name)
    if provider is None:
        raise ValueError(f"unknown realtime voice provider: {provider_name}")
    missing_capabilities = required_capabilities - provider.capabilities
    if missing_capabilities:
        raise UnsupportedRealtimeCapability(
            min(missing_capabilities, key=lambda capability: capability.value)
        )
    if not provider.is_available():
        raise RuntimeError(f"realtime voice provider is unavailable: {provider_name}")
    return await provider.open_session(setup)


class RealtimeVoiceOrchestrator:
    """Resolve and supervise a provider session for an owning Hermes surface."""

    def __init__(self, host: RealtimeVoiceHost) -> None:
        self._host = host

    async def run(
        self,
        provider_name: str,
        setup: RealtimeVoiceSetup,
        *,
        required_capabilities: frozenset[RealtimeCapability] = frozenset(),
    ) -> None:
        session = await open_realtime_voice_session(
            provider_name,
            setup,
            required_capabilities=required_capabilities,
        )
        async with session:
            async for event in session.events():
                await self._host.handle_realtime_event(event)


__all__ = [
    "RealtimeVoiceHost",
    "RealtimeVoiceOrchestrator",
    "open_realtime_voice_session",
]
