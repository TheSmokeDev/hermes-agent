"""Provider-neutral supervision for one Realtime voice session."""

from __future__ import annotations

from typing import Protocol

from agent.realtime_voice_provider import (
    RealtimeCapability,
    RealtimeVoiceEvent,
    RealtimeVoiceSetup,
    UnsupportedRealtimeCapability,
)
from agent.realtime_voice_registry import get_provider


class RealtimeVoiceHost(Protocol):
    """Owning Hermes surface that receives normalized provider events."""

    async def handle_realtime_event(self, event: RealtimeVoiceEvent) -> None:
        """Handle one normalized event without transferring host authority."""


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
        provider = get_provider(provider_name)
        if provider is None:
            raise ValueError(f"unknown realtime voice provider: {provider_name}")
        missing_capabilities = required_capabilities - provider.capabilities
        if missing_capabilities:
            raise UnsupportedRealtimeCapability(
                min(missing_capabilities, key=lambda capability: capability.value)
            )

        session = await provider.open_session(setup)
        async with session:
            async for event in session.events():
                await self._host.handle_realtime_event(event)


__all__ = ["RealtimeVoiceHost", "RealtimeVoiceOrchestrator"]
