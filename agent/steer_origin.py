"""Server-created row bindings carried through the existing pending-steer text slot."""
from __future__ import annotations

import json


class _BoundSteerText(str):
    def __new__(cls, parts):
        value = super().__new__(cls, "\n".join(text for text, _ in parts))
        value.parts = tuple(parts)
        return value


def bound_steer_text(text, message):
    # Only host-verified, born-durable row copies reach this factory; HTTP never accepts this type.
    return _BoundSteerText(((text, json.dumps(message)),))


def combine_steer_text(left, right):
    if not isinstance(left, _BoundSteerText) and not isinstance(right, _BoundSteerText):
        return (left + "\n" + right) if left else right
    parts = []
    for value in (left, right):
        for text, row in value.parts if isinstance(value, _BoundSteerText) else ((value, None),):
            if not text:
                continue
            if parts and row is None and parts[-1][1] is None:
                parts[-1] = (parts[-1][0] + "\n" + text, None)
            else:
                parts.append((text, row))
    return _BoundSteerText(parts)


def clean_steer_text(text):
    return text if isinstance(text, _BoundSteerText) else text.strip()


def bound_steer_rows(text):
    if not isinstance(text, _BoundSteerText):
        return None
    from agent.prompt_builder import steer_user_row
    return [json.loads(row) if row is not None else steer_user_row(part) for part, row in text.parts]


def has_bound_steer(text):
    return isinstance(text, _BoundSteerText)


def unbound_steer_text(text):
    """Only legacy unbound text may become a fresh next-turn prompt after a missed drain."""
    if not isinstance(text, _BoundSteerText):
        return text
    return "\n".join(part for part, row in text.parts if row is None) or None
