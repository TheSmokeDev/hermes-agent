"""Opaque host-owned capability for canonical Realtime execution.

This module deliberately exposes no routing, identity, session, runner, or
execution schema.  The host keeps all authority in private identity maps.
"""

from __future__ import annotations


_MINT = object()


class RealtimeToolCallPermit:
    """Opaque host-minted receipt for one provider tool-call admission."""

    __slots__ = ("__weakref__",)

    def __new__(cls, mint: object = None):
        if mint is not _MINT:
            raise TypeError("realtime tool call permits are host-minted")
        return super().__new__(cls)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("realtime tool call permits are immutable")

    def __repr__(self) -> str:
        return "<host realtime tool call permit>"

    def __reduce__(self):
        raise TypeError("realtime tool call permits cannot be serialized")


class RealtimeExecutionAttachment:
    """Narrow nonserializable lifecycle handle minted by an authenticated host."""

    __slots__ = ("__weakref__",)

    def __new__(cls, mint: object = None):
        if mint is not _MINT:
            raise TypeError("realtime execution attachments are host-minted")
        return super().__new__(cls)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("realtime execution attachments are immutable")

    def __repr__(self) -> str:
        return "<host realtime execution attachment>"

    def __reduce__(self):
        raise TypeError("realtime execution attachments cannot be serialized")

    @property
    def closed(self) -> bool:
        """Whether this attachment no longer carries future host authority."""

        from gateway.realtime_voice_invocation import (
            _is_realtime_execution_attachment_closed,
        )

        return _is_realtime_execution_attachment_closed(self)

    def close(self) -> None:
        """Revoke unused future authority without affecting accepted work."""

        from gateway.realtime_voice_invocation import _close_realtime_execution_attachment

        _close_realtime_execution_attachment(self)

    def tool_definitions(self) -> list[dict[str, object]]:
        """Return a detached provider-neutral snapshot of host-curated tools."""

        from gateway.realtime_voice_invocation import (
            _tool_definitions_for_realtime_execution_attachment,
        )

        return _tool_definitions_for_realtime_execution_attachment(self)

    def mint_tool_call_permit(
        self,
        *,
        response_id: str,
        item_id: str,
        call_id: str,
        batch_id: str,
        tool_name: str,
        arguments: dict[str, object],
    ) -> RealtimeToolCallPermit:
        """Mint one opaque admission receipt; this does not execute a tool."""

        from gateway.realtime_voice_invocation import (
            _mint_tool_call_permit_for_realtime_execution_attachment,
        )

        return _mint_tool_call_permit_for_realtime_execution_attachment(
            self,
            response_id=response_id,
            item_id=item_id,
            call_id=call_id,
            batch_id=batch_id,
            tool_name=tool_name,
            arguments=arguments,
        )


def _mint_realtime_execution_attachment() -> RealtimeExecutionAttachment:
    return RealtimeExecutionAttachment(_MINT)


def _mint_realtime_tool_call_permit() -> RealtimeToolCallPermit:
    return RealtimeToolCallPermit(_MINT)
