"""Opaque host-owned capability for canonical Realtime execution.

This module deliberately exposes no routing, identity, session, runner, or
execution schema.  The host keeps all authority in private identity maps.
"""

from __future__ import annotations


_MINT = object()


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


def _mint_realtime_execution_attachment() -> RealtimeExecutionAttachment:
    return RealtimeExecutionAttachment(_MINT)
