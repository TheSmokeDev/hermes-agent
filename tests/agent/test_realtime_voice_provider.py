"""Behavior tests for the typed, provider-neutral realtime voice contract."""

from __future__ import annotations

import asyncio
import hashlib

import pytest

import agent.realtime_voice_provider as realtime_voice_provider
from agent.realtime_voice_provider import (
    InputTranscript,
    Interruption,
    OutputAudio,
    OutputTranscript,
    RealtimeCapability,
    RealtimeAudioFormat,
    RealtimeInputAudioFormat,
    RealtimeOutputAudioFormat,
    RealtimeResponseRequest,
    ResponseCompleted,
    ResponseStarted,
    SessionClosed,
    SessionFailure,
    SessionReady,
    ToolCall,
    ToolCallCancelled,
    RealtimeTool,
    RealtimeToolResult,
    RealtimeVoiceEvent,
    RealtimeVoiceSession,
    RealtimeVoiceSetup,
    TranscriptProvenance,
    TranscriptRole,
    TurnCompleted,
    TurnStarted,
    UnsupportedRealtimeCapability,
    MAX_IDENTIFIER_LENGTH,
)


def test_setup_copies_and_freezes_provider_options() -> None:
    tags = {"local"}
    options = {"region": "local", "tags": tags}

    setup = RealtimeVoiceSetup(model="model", voice="voice", provider_options=options)
    options["region"] = "changed"
    tags.add("mutated")

    assert setup.provider_options == {"region": "local", "tags": frozenset({"local"})}


def test_setup_preserves_legacy_sixth_positional_provider_options() -> None:
    options = {"region": "local", "nested": ["value"]}

    setup = RealtimeVoiceSetup(
        "model",
        "voice",
        "instructions",
        (),
        RealtimeAudioFormat("audio/pcm", 24_000, 1),
        options,
    )
    options["nested"].append("mutated")

    assert setup.provider_options == {"region": "local", "nested": ("value",)}
    assert setup.input_audio is None
    assert setup.output_audio is None
    assert setup.automatic_response is False


def test_opaque_mappings_reject_values_that_cannot_be_frozen() -> None:
    class Mutable:
        pass

    with pytest.raises(TypeError, match="provider-neutral immutable value"):
        RealtimeVoiceSetup(provider_options={"native": Mutable()})
    with pytest.raises(TypeError, match="provider-neutral immutable value"):
        SessionReady(session_id="session", provider_data={"native": Mutable()})


def test_setup_rejects_provider_options_that_shadow_shared_fields() -> None:
    with pytest.raises(ValueError, match="shared setup field"):
        RealtimeVoiceSetup(model="model", provider_options={"model": "shadow"})


@pytest.mark.parametrize("value", ["", " padded", "padded "])
def test_setup_validates_declared_model_voice_and_tool_names(value: str) -> None:
    with pytest.raises(ValueError, match="model"):
        RealtimeVoiceSetup(model=value)
    with pytest.raises(ValueError, match="voice"):
        RealtimeVoiceSetup(voice=value)
    with pytest.raises(ValueError, match="name"):
        RealtimeTool(name=value, description="description", parameters={})


def test_setup_declares_shared_audio_instructions_and_tools_immutably() -> None:
    schema = {"type": "object"}
    tools = [RealtimeTool(name="lookup", description="Look up", parameters=schema)]
    audio = RealtimeAudioFormat(mime_type="audio/pcm", sample_rate_hz=24_000, channels=1)

    setup = RealtimeVoiceSetup(
        model="model",
        voice="voice",
        instructions="Be concise",
        tools=tools,
        audio=audio,
    )
    schema["type"] = "changed"
    tools.clear()

    assert setup.tools[0].parameters == {"type": "object"}
    assert setup.audio is audio


@pytest.mark.parametrize(
    "factory",
    [
        lambda value: SessionReady(session_id="session", provider_data=value),
        lambda value: RealtimeVoiceSetup(provider_options=value),
        lambda value: ToolCall(
            "call", "batch", "turn", "response", "lookup", value
        ),
        lambda value: RealtimeTool("lookup", "description", value),
    ],
)
def test_mapping_contract_fields_require_mapping_shape_and_string_keys(factory) -> None:
    with pytest.raises(TypeError, match="Mapping"):
        factory([])
    with pytest.raises(TypeError, match="string keys"):
        factory({1: "value"})


@pytest.mark.parametrize("mime_type", ["", " audio/pcm", "audio/pcm ", "x" * 513])
def test_audio_format_rejects_blank_padded_or_oversized_mime_type(mime_type) -> None:
    with pytest.raises(ValueError, match="mime_type"):
        RealtimeAudioFormat(mime_type=mime_type, sample_rate_hz=24_000, channels=1)


@pytest.mark.parametrize("field_name", ["sample_rate_hz", "channels"])
@pytest.mark.parametrize("invalid", [0, -1, True, 1.5, "1", None])
def test_audio_format_requires_positive_non_bool_integer_primitives(
    field_name, invalid
) -> None:
    values = {"mime_type": "audio/pcm", "sample_rate_hz": 24_000, "channels": 1}
    values[field_name] = invalid

    with pytest.raises((TypeError, ValueError), match=field_name):
        RealtimeAudioFormat(**values)


def test_setup_distinguishes_exact_input_and_output_audio_from_legacy_audio() -> None:
    legacy = RealtimeAudioFormat("audio/pcm", 24_000, 1)
    input_audio = RealtimeInputAudioFormat(
        "audio/pcm", 24_000, 1, "pcm_s16le", 2, "little"
    )
    output_audio = RealtimeOutputAudioFormat(
        "audio/pcm", 24_000, 1, "pcm_s16le", 2, "little"
    )

    setup = RealtimeVoiceSetup(
        audio=legacy,
        input_audio=input_audio,
        output_audio=output_audio,
    )

    assert setup.audio is legacy
    assert setup.input_audio is input_audio
    assert setup.output_audio is output_audio
    assert setup.automatic_response is False
    with pytest.raises(ValueError, match="sample_width_bytes"):
        RealtimeOutputAudioFormat(
            "audio/pcm", 24_000, 1, "pcm_s16le", 4, "little"
        )
    with pytest.raises(ValueError, match="endianness"):
        RealtimeOutputAudioFormat(
            "audio/pcm", 24_000, 1, "pcm_s16le", 2, "big"
        )


def test_exact_audio_formats_reject_numeric_lookalikes_and_pcm_contradictions() -> None:
    class IntLookalike(int):
        pass

    with pytest.raises(TypeError, match="sample_rate_hz"):
        RealtimeInputAudioFormat("audio/pcm", IntLookalike(24_000), 1)
    with pytest.raises(TypeError, match="sample_width_bytes"):
        RealtimeOutputAudioFormat(
            "audio/pcm", 24_000, 1, "pcm_s16le", IntLookalike(2), "little"
        )
    with pytest.raises(ValueError, match="mime_type"):
        RealtimeOutputAudioFormat(
            "audio/wav", 24_000, 1, "pcm_s16le", 2, "little"
        )


def test_exact_audio_formats_require_every_exact_noncoercive_dimension() -> None:
    class StrLookalike(str):
        pass

    class IntLookalike(int):
        pass

    valid = {
        "mime_type": "audio/opus",
        "sample_rate_hz": 24_000,
        "channels": 1,
        "sample_encoding": "opus",
        "sample_width_bytes": 2,
        "endianness": "little",
    }
    invalid_dimensions = (
        ("sample_encoding", None),
        ("sample_encoding", StrLookalike("pcm_s16le")),
        ("sample_width_bytes", None),
        ("sample_width_bytes", "2"),
        ("sample_width_bytes", IntLookalike(2)),
        ("endianness", None),
        ("endianness", StrLookalike("little")),
        ("sample_rate_hz", "24000"),
        ("sample_rate_hz", IntLookalike(24_000)),
        ("channels", "1"),
        ("channels", IntLookalike(1)),
    )

    for format_type in (RealtimeInputAudioFormat, RealtimeOutputAudioFormat):
        for field_name, invalid in invalid_dimensions:
            values = dict(valid)
            values[field_name] = invalid
            with pytest.raises((TypeError, ValueError), match=field_name):
                format_type(**values)


@pytest.mark.parametrize("instructions", [1, b"text", object()])
def test_setup_instructions_must_be_none_or_string(instructions) -> None:
    with pytest.raises(TypeError, match="instructions"):
        RealtimeVoiceSetup(instructions=instructions)


@pytest.mark.parametrize("audio", [{}, "audio/pcm", object()])
def test_setup_audio_must_be_none_or_realtime_audio_format(audio) -> None:
    with pytest.raises(TypeError, match="audio"):
        RealtimeVoiceSetup(audio=audio)


@pytest.mark.parametrize("tools", ["lookup", b"lookup", [object()]])
def test_setup_tools_reject_strings_and_non_tools(tools) -> None:
    with pytest.raises(TypeError, match="tools"):
        RealtimeVoiceSetup(tools=tools)


def test_setup_copies_tool_iterables_to_tuple() -> None:
    tool = RealtimeTool("lookup", "description", {})
    setup = RealtimeVoiceSetup(tools=(item for item in [tool]))

    assert setup.tools == (tool,)


def test_capabilities_are_explicit_and_immutable() -> None:
    capabilities = frozenset(RealtimeCapability)

    assert {capability.value for capability in capabilities} == {
        "tool_calling",
        "explicit_interruption",
        "output_truncation",
        "input_commit_events",
        "response_metadata_echo",
        "tool_call_cancellation",
        "dynamic_context",
        "session_resumption",
        "input_transcription",
        "output_transcription",
        "continuation",
        "explicit_response",
    }


def test_response_request_is_immutable_and_causally_bound_to_canonical_text() -> None:
    text = " Canonical assistant reply. "
    output_format = RealtimeOutputAudioFormat(
        mime_type="audio/pcm",
        sample_encoding="pcm_s16le",
        sample_width_bytes=2,
        endianness="little",
        sample_rate_hz=24_000,
        channels=1,
    )

    request = RealtimeResponseRequest(
        durable_session_id="durable-session",
        assistant_message_id=17,
        turn_marker="turn-marker",
        canonical_text=text,
        content_digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        output_audio_format=output_format,
        allow_tools=False,
    )

    assert request.output_audio_format is output_format
    assert request.allow_tools is False
    with pytest.raises(AttributeError):
        request.turn_marker = "changed"


def test_response_request_rejects_noncanonical_causal_identity_and_digest() -> None:
    text = "Canonical reply"
    values = {
        "durable_session_id": "durable",
        "assistant_message_id": 1,
        "turn_marker": "turn",
        "canonical_text": text,
        "content_digest": hashlib.sha256(text.encode()).hexdigest(),
        "output_audio_format": RealtimeOutputAudioFormat(
            mime_type="audio/pcm",
            sample_encoding="pcm_s16le",
            sample_width_bytes=2,
            endianness="little",
            sample_rate_hz=24_000,
            channels=1,
        ),
        "allow_tools": False,
    }

    invalid_values = (
        ("durable_session_id", " padded"),
        ("assistant_message_id", True),
        ("assistant_message_id", 0),
        ("turn_marker", ""),
        ("canonical_text", " \t"),
        ("canonical_text", "\ud800"),
        ("content_digest", "A" * 64),
        ("content_digest", "0" * 64),
        ("allow_tools", True),
        ("allow_tools", 0),
    )
    for field_name, invalid in invalid_values:
        candidate = dict(values)
        candidate[field_name] = invalid
        with pytest.raises((TypeError, ValueError), match=field_name):
            RealtimeResponseRequest(**candidate)


def test_shared_event_vocabulary_is_typed_and_provider_data_is_opaque() -> None:
    tags = {"tag"}
    provider_data = {"native": {"values": ["value"], "tags": tags}}
    events = [
        SessionReady(session_id="session", provider_data=provider_data),
        SessionClosed(reason="normal"),
        SessionFailure(code="transport", message="lost"),
        InputTranscript(
            item_id="input",
            turn_id="turn",
            text="hello",
            final=True,
            role=TranscriptRole.PARTICIPANT,
            provenance=TranscriptProvenance.PARTICIPANT_INPUT_AUDIO,
        ),
        OutputTranscript(
            item_id="output",
            turn_id="turn",
            response_id="response",
            text="hi",
            final=False,
        ),
        OutputAudio(
            data=b"pcm",
            item_id="output",
            turn_id="turn",
            response_id="response",
        ),
        ToolCall(call_id="call", batch_id="batch", turn_id="turn", response_id="response", name="lookup", arguments={"q": "x"}),
        ToolCallCancelled(call_id="call", batch_id="batch"),
        TurnStarted(turn_id="turn"),
        TurnCompleted(turn_id="turn"),
        ResponseStarted(response_id="response", turn_id="turn"),
        ResponseCompleted(response_id="response", turn_id="turn"),
        Interruption(response_id="response", turn_id="turn"),
    ]
    provider_data["native"]["values"].append("changed")
    tags.add("mutated")

    assert len(events) == 13
    assert events[0].session_id == "session"
    assert events[0].provider_data == {
        "native": {"values": ("value",), "tags": frozenset({"tag"})}
    }


def test_output_audio_copies_mutable_buffers() -> None:
    source = bytearray(b"abc")
    event = OutputAudio(
        data=source, item_id="item", turn_id="turn", response_id="response"
    )

    source[0] = ord("z")

    assert event.data == b"abc"
    assert isinstance(event.data, bytes)


def test_provider_data_cannot_shadow_shared_event_fields() -> None:
    with pytest.raises(ValueError, match="provider_data.*session_id"):
        SessionReady(session_id="session", provider_data={"session_id": "spoof"})


@pytest.mark.parametrize("session_id", ["", " padded", "padded "])
def test_shared_events_reject_blank_or_malformed_identifiers(session_id: str) -> None:
    with pytest.raises(ValueError, match="session_id"):
        SessionReady(session_id=session_id)


def test_shared_events_reject_oversized_identifiers() -> None:
    with pytest.raises(ValueError, match="session_id"):
        SessionReady(session_id="x" * (MAX_IDENTIFIER_LENGTH + 1))


@pytest.mark.parametrize(
    ("factory", "field_name"),
    [
        (
            lambda value: InputTranscript(
                value,
                "turn",
                "text",
                True,
                TranscriptRole.OPERATOR,
                TranscriptProvenance.OPERATOR_INPUT,
            ),
            "item_id",
        ),
        (
            lambda value: OutputTranscript(
                value, "turn", "response", "text", True
            ),
            "item_id",
        ),
        (
            lambda value: OutputAudio(b"pcm", value, "turn", "response"),
            "item_id",
        ),
        (lambda value: ToolCall(value, "batch", "turn", "response", "lookup", {}), "call_id"),
        (lambda value: ToolCall("call", "batch", "turn", "response", value, {}), "name"),
        (lambda value: ToolCallCancelled(value, "batch"), "call_id"),
        (lambda value: TurnStarted(value), "turn_id"),
        (lambda value: TurnCompleted(value), "turn_id"),
        (lambda value: ResponseStarted(value, "turn"), "response_id"),
        (lambda value: ResponseCompleted(value, "turn"), "response_id"),
        (lambda value: Interruption(value, "turn"), "response_id"),
        (lambda value: SessionFailure(value, "message"), "code"),
    ],
)
def test_all_required_shared_identifiers_and_names_are_validated(factory, field_name) -> None:
    for invalid in ("", " padded", "padded ", "x" * (MAX_IDENTIFIER_LENGTH + 1)):
        with pytest.raises(ValueError, match=field_name):
            factory(invalid)


def test_transcript_attribution_rejects_contradictory_provenance() -> None:
    with pytest.raises(ValueError, match="role.*provenance"):
        InputTranscript(
            item_id="input",
            turn_id="turn",
            text="spoofed",
            final=True,
            role=TranscriptRole.ASSISTANT,
            provenance=TranscriptProvenance.PARTICIPANT_INPUT_AUDIO,
        )

    with pytest.raises(ValueError, match="role.*provenance"):
        OutputTranscript(
            item_id="output",
            turn_id="turn",
            response_id="response",
            text="spoofed",
            final=True,
            role=TranscriptRole.OPERATOR,
            provenance=TranscriptProvenance.ASSISTANT_OUTPUT_AUDIO,
        )


def test_tool_work_has_stable_neutral_batch_and_response_identity() -> None:
    arguments = {"nested": ["value"]}
    output = {"items": [1]}

    call = ToolCall(
        call_id="call",
        batch_id="batch",
        turn_id="turn",
        response_id="response",
        name="lookup",
        arguments=arguments,
    )
    cancelled = ToolCallCancelled(call_id="call", batch_id="batch")
    result = RealtimeToolResult(
        call_id="call", batch_id="batch", name="lookup", output=output
    )
    continuation = ResponseStarted(
        response_id="next-response",
        turn_id="turn",
        continuation_of_batch_id="batch",
    )
    arguments["nested"].append("mutated")
    output["items"].append(2)

    assert call.arguments == {"nested": ("value",)}
    assert cancelled.batch_id == result.batch_id == "batch"
    assert result.output == {"items": (1,)}
    assert continuation.continuation_of_batch_id == "batch"


def test_transcript_audio_and_terminal_events_have_explicit_association() -> None:
    input_event = InputTranscript(
        item_id="input",
        turn_id="turn",
        text="hello",
        final=True,
        role=TranscriptRole.OPERATOR,
        provenance=TranscriptProvenance.OPERATOR_INPUT,
    )
    transcript = OutputTranscript(
        item_id="output", turn_id="turn", response_id="response", text="hi", final=True
    )
    audio = OutputAudio(
        data=b"pcm", item_id="output", turn_id="turn", response_id="response"
    )
    completed = ResponseCompleted(
        response_id="response", turn_id="turn", continuation_of_batch_id="batch"
    )
    interrupted = Interruption(response_id="response", turn_id="turn")

    assert input_event.turn_id == transcript.turn_id == audio.turn_id == "turn"
    assert transcript.response_id == audio.response_id == completed.response_id
    assert completed.continuation_of_batch_id == "batch"
    assert interrupted.turn_id == "turn"


class _Session(RealtimeVoiceSession):
    def __init__(self, capabilities=()) -> None:
        super().__init__(capabilities)
        self.closed_count = 0
        self.submitted = []
        self.continuations = []
        self.event_values = []
        self.response_requests = []

    async def send_audio(self, audio: bytes, *, mime_type: str | None = None) -> None:
        return None

    async def _submit_tool_results(self, batch_id, results) -> None:
        self.submitted.append((batch_id, results))

    def _events(self):
        async def stream():
            for event in self.event_values:
                yield event

        return stream()

    async def _continue_response(self, batch_id: str) -> None:
        self.continuations.append(batch_id)

    async def _start_response(self, request: RealtimeResponseRequest) -> None:
        self.response_requests.append(request)

    async def _close(self) -> None:
        await asyncio.sleep(0)
        self.closed_count += 1


class _ControlledCloseSession(_Session):
    def __init__(self, outcomes=()) -> None:
        super().__init__()
        self.close_entered = asyncio.Event()
        self.release_close = asyncio.Event()
        self.outcomes = list(outcomes)
        self.completed_close_count = 0

    async def _close(self) -> None:
        self.closed_count += 1
        self.close_entered.set()
        await self.release_close.wait()
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if outcome is not None:
                raise outcome
        self.completed_close_count += 1


class _FailingContinuationSession(_Session):
    def __init__(self) -> None:
        super().__init__({RealtimeCapability.CONTINUATION})
        self.continue_entered = asyncio.Event()
        self.release_continue = asyncio.Event()

    async def _continue_response(self, batch_id: str) -> None:
        self.continuations.append(batch_id)
        self.continue_entered.set()
        await self.release_continue.wait()
        raise RuntimeError("continuation write failed")


class _FailingResponseSession(_Session):
    def __init__(self) -> None:
        super().__init__({RealtimeCapability.EXPLICIT_RESPONSE})

    async def _start_response(self, request: RealtimeResponseRequest) -> None:
        self.response_requests.append(request)
        raise RuntimeError("native response write failed")


class _NoOptionalHooksSession(RealtimeVoiceSession):
    async def send_audio(self, audio: bytes, *, mime_type: str | None = None) -> None:
        return None

    async def _submit_tool_results(self, batch_id, results) -> None:
        return None

    def _events(self):
        async def stream():
            if False:
                yield RealtimeVoiceEvent()

        return stream()

    async def _close(self) -> None:
        return None


def _response_request(
    *, assistant_message_id: int = 1, canonical_text: str = "Canonical reply"
) -> RealtimeResponseRequest:
    return RealtimeResponseRequest(
        durable_session_id="durable",
        assistant_message_id=assistant_message_id,
        turn_marker=f"turn-{assistant_message_id}",
        canonical_text=canonical_text,
        content_digest=hashlib.sha256(canonical_text.encode()).hexdigest(),
        output_audio_format=RealtimeOutputAudioFormat(
            "audio/pcm", 24_000, 1, "pcm_s16le", 2, "little"
        ),
        allow_tools=False,
    )


@pytest.mark.asyncio
async def test_start_response_is_capability_gated_exact_typed_and_replay_safe() -> None:
    request = _response_request()
    unsupported = _Session()
    with pytest.raises(UnsupportedRealtimeCapability) as exc_info:
        await unsupported.start_response(request)
    assert exc_info.value.capability is RealtimeCapability.EXPLICIT_RESPONSE
    assert unsupported.response_requests == []

    session = _Session({RealtimeCapability.EXPLICIT_RESPONSE})
    with pytest.raises(TypeError, match="RealtimeResponseRequest"):
        await session.start_response(object())
    await session.start_response(request)
    assert session.response_requests == [request]
    with pytest.raises(ValueError, match="already accepted/sent"):
        await session.start_response(request)
    assert session.response_requests == [request]

    closed = _Session({RealtimeCapability.EXPLICIT_RESPONSE})
    await closed.close()
    with pytest.raises(RuntimeError, match="closed"):
        await closed.start_response(_response_request(assistant_message_id=2))
    assert closed.response_requests == []


@pytest.mark.asyncio
async def test_start_response_replay_history_fails_closed_at_exact_capacity(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        realtime_voice_provider, "MAX_ACCEPTED_EXPLICIT_RESPONSE_SEND_TOMBSTONES", 2
    )
    session = _Session({RealtimeCapability.EXPLICIT_RESPONSE})
    oldest = _response_request(assistant_message_id=1)
    newest = _response_request(assistant_message_id=2)
    overflow = _response_request(assistant_message_id=3)

    await session.start_response(oldest)
    await session.start_response(newest)

    with pytest.raises(ValueError, match="already accepted/sent"):
        await session.start_response(oldest)
    with pytest.raises(ValueError, match="replay tracking limit"):
        await session.start_response(overflow)
    with pytest.raises(ValueError, match="already accepted/sent"):
        await session.start_response(oldest)

    assert session.response_requests == [oldest, newest]


@pytest.mark.asyncio
async def test_explicit_response_bookkeeping_names_only_request_send_state() -> None:
    session = _Session({RealtimeCapability.EXPLICIT_RESPONSE})
    request = _response_request()

    assert session._in_flight_response_sends == set()
    assert session._accepted_response_send_tombstones == {}
    assert not hasattr(session, "_active_response_requests")
    assert not hasattr(session, "_completed_response_requests")

    await session.start_response(request)

    assert session._in_flight_response_sends == set()
    assert list(session._accepted_response_send_tombstones) == [request]


@pytest.mark.asyncio
async def test_start_response_failure_is_terminal_and_host_visible() -> None:
    session = _FailingResponseSession()
    request = _response_request()

    with pytest.raises(RuntimeError, match="native response write failed"):
        await session.start_response(request)

    assert session.closed_count == 1
    events = [event async for event in session.events()]
    assert len(events) == 1
    assert isinstance(events[0], SessionFailure)
    assert events[0].code == "explicit_response_failed"
    with pytest.raises(RuntimeError, match="closed"):
        await session.start_response(_response_request(assistant_message_id=2))
    assert session.response_requests == [request]


@pytest.mark.asyncio
async def test_tool_results_require_tool_calling_and_continuation_is_separate() -> None:
    result = RealtimeToolResult(
        call_id="call", batch_id="batch", name="lookup", output="done"
    )
    unsupported = _Session()

    with pytest.raises(UnsupportedRealtimeCapability) as exc:
        await unsupported.submit_tool_results("batch", [result])
    assert exc.value.capability is RealtimeCapability.TOOL_CALLING

    session = _Session(
        {RealtimeCapability.TOOL_CALLING, RealtimeCapability.CONTINUATION}
    )
    await session.submit_tool_results("batch", [result])
    assert session.submitted == [("batch", (result,))]
    assert session.continuations == []

    await session.continue_response("batch")
    assert session.continuations == ["batch"]


@pytest.mark.asyncio
async def test_requested_continuation_requires_linked_response_start() -> None:
    session = _Session({RealtimeCapability.CONTINUATION})
    session.event_values = [
        ResponseStarted(response_id="next-response", turn_id="turn")
    ]

    await session.continue_response("batch")

    events = session.events()
    assert await anext(events) == session.event_values[0]
    with pytest.raises(ValueError, match="unresolved continuation"):
        await anext(events)


@pytest.mark.asyncio
async def test_terminal_event_resolves_outstanding_continuation_state() -> None:
    failure = SessionFailure(code="transport", message="connection lost")
    session = _Session({RealtimeCapability.CONTINUATION})
    session.event_values = [failure]
    await session.continue_response("batch")

    assert [event async for event in session.events()] == [failure]
    assert not session._pending_continuation_batch_ids
    assert not session._continuation_responses


@pytest.mark.asyncio
async def test_requested_continuation_links_response_start_and_completion() -> None:
    session = _Session({RealtimeCapability.CONTINUATION})
    started = ResponseStarted(
        response_id="next-response",
        turn_id="turn",
        continuation_of_batch_id="batch",
    )
    completed = ResponseCompleted(
        response_id="next-response",
        turn_id="turn",
        continuation_of_batch_id="batch",
    )
    session.event_values = [started, completed]

    await session.continue_response("batch")

    assert [event async for event in session.events()] == [started, completed]


@pytest.mark.asyncio
async def test_concurrent_continuations_match_by_batch_not_response_order() -> None:
    session = _Session({RealtimeCapability.CONTINUATION})
    ordinary = ResponseStarted(response_id="ordinary-response", turn_id="turn")
    started_b = ResponseStarted(
        response_id="response-b",
        turn_id="turn",
        continuation_of_batch_id="batch-b",
    )
    completed_b = ResponseCompleted(
        response_id="response-b",
        turn_id="turn",
        continuation_of_batch_id="batch-b",
    )
    started_a = ResponseStarted(
        response_id="response-a",
        turn_id="turn",
        continuation_of_batch_id="batch-a",
    )
    completed_a = ResponseCompleted(
        response_id="response-a",
        turn_id="turn",
        continuation_of_batch_id="batch-a",
    )
    session.event_values = [ordinary, started_b, completed_b, started_a, completed_a]

    await session.continue_response("batch-a")
    await session.continue_response("batch-b")

    assert [event async for event in session.events()] == session.event_values


@pytest.mark.asyncio
async def test_ordinary_response_redelivery_cannot_acquire_continuation_link() -> None:
    ordinary = ResponseStarted(response_id="same", turn_id="turn")
    upgraded_duplicate = ResponseStarted(
        response_id="same",
        turn_id="turn",
        continuation_of_batch_id="batch",
    )
    session = _Session({RealtimeCapability.CONTINUATION})
    session.event_values = [ordinary, upgraded_duplicate]
    await session.continue_response("batch")

    events = session.events()
    assert await anext(events) == ordinary
    with pytest.raises(ValueError, match="linkage changed"):
        await anext(events)


@pytest.mark.asyncio
async def test_ordinary_response_upgrade_does_not_consume_pending_batch() -> None:
    ordinary = ResponseStarted(response_id="same", turn_id="turn")
    upgraded_duplicate = ResponseStarted(
        response_id="same",
        turn_id="turn",
        continuation_of_batch_id="batch",
    )
    session = _Session({RealtimeCapability.CONTINUATION})
    await session.continue_response("batch")

    session._validate_continuation_event(ordinary)
    with pytest.raises(ValueError, match="linkage changed"):
        session._validate_continuation_event(upgraded_duplicate)

    assert list(session._pending_continuation_batch_ids) == ["batch"]


@pytest.mark.asyncio
async def test_ordinary_response_completion_cannot_acquire_continuation_link() -> None:
    ordinary = ResponseStarted(response_id="same", turn_id="turn")
    upgraded_completion = ResponseCompleted(
        response_id="same",
        turn_id="turn",
        continuation_of_batch_id="batch",
    )
    session = _Session({RealtimeCapability.CONTINUATION})
    session.event_values = [ordinary, upgraded_completion]
    await session.continue_response("batch")

    events = session.events()
    assert await anext(events) == ordinary
    with pytest.raises(ValueError, match="linkage changed"):
        await anext(events)


@pytest.mark.asyncio
async def test_completion_first_ordinary_response_cannot_later_acquire_link() -> None:
    ordinary_completion = ResponseCompleted(response_id="same", turn_id="turn")
    upgraded_start = ResponseStarted(
        response_id="same",
        turn_id="turn",
        continuation_of_batch_id="batch",
    )
    session = _Session({RealtimeCapability.CONTINUATION})
    session.event_values = [ordinary_completion, upgraded_start]
    await session.continue_response("batch")

    events = session.events()
    assert await anext(events) == ordinary_completion
    with pytest.raises(ValueError, match="linkage changed"):
        await anext(events)


@pytest.mark.asyncio
async def test_ordinary_response_identity_history_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(
        realtime_voice_provider, "MAX_TRACKED_ORDINARY_RESPONSES", 2
    )
    session = _Session({RealtimeCapability.CONTINUATION})
    session.event_values = [
        ResponseStarted(response_id=f"response-{index}", turn_id="turn")
        for index in range(3)
    ]

    assert [event async for event in session.events()] == session.event_values
    assert list(session._ordinary_response_ids) == ["response-1", "response-2"]


@pytest.mark.asyncio
async def test_event_stream_eof_rejects_unresolved_continuations() -> None:
    pending = _Session({RealtimeCapability.CONTINUATION})
    await pending.continue_response("pending-batch")
    with pytest.raises(ValueError, match="unresolved continuation"):
        assert [event async for event in pending.events()] == []

    active = _Session({RealtimeCapability.CONTINUATION})
    active.event_values = [
        ResponseStarted(
            response_id="active-response",
            turn_id="turn",
            continuation_of_batch_id="active-batch",
        )
    ]
    await active.continue_response("active-batch")
    with pytest.raises(ValueError, match="unresolved continuation"):
        assert [event async for event in active.events()] == active.event_values


@pytest.mark.asyncio
async def test_outstanding_continuations_are_bounded(monkeypatch) -> None:
    monkeypatch.setattr(
        realtime_voice_provider, "MAX_OUTSTANDING_CONTINUATIONS", 2
    )
    session = _Session({RealtimeCapability.CONTINUATION})

    await session.continue_response("batch-a")
    await session.continue_response("batch-b")

    with pytest.raises(ValueError, match="outstanding continuation limit"):
        await session.continue_response("batch-c")


@pytest.mark.asyncio
async def test_identical_continuation_redelivery_reaches_host_for_batch_dedupe() -> None:
    session = _Session({RealtimeCapability.CONTINUATION})
    started = ResponseStarted(
        response_id="next-response",
        turn_id="turn",
        continuation_of_batch_id="batch",
    )
    completed = ResponseCompleted(
        response_id="next-response",
        turn_id="turn",
        continuation_of_batch_id="batch",
    )
    session.event_values = [started, started, completed, completed]

    await session.continue_response("batch")

    assert [event async for event in session.events()] == session.event_values


@pytest.mark.asyncio
async def test_continuation_redelivery_cannot_drop_batch_linkage() -> None:
    started = ResponseStarted(
        response_id="next-response",
        turn_id="turn",
        continuation_of_batch_id="batch",
    )
    completed = ResponseCompleted(
        response_id="next-response",
        turn_id="turn",
        continuation_of_batch_id="batch",
    )

    start_session = _Session({RealtimeCapability.CONTINUATION})
    start_session.event_values = [
        started,
        ResponseStarted(response_id="next-response", turn_id="turn"),
    ]
    await start_session.continue_response("batch")
    start_events = start_session.events()
    assert await anext(start_events) == started
    with pytest.raises(ValueError, match="continuation response"):
        await anext(start_events)

    completion_session = _Session({RealtimeCapability.CONTINUATION})
    completion_session.event_values = [
        started,
        completed,
        ResponseCompleted(response_id="next-response", turn_id="turn"),
    ]
    await completion_session.continue_response("batch")
    completion_events = completion_session.events()
    assert await anext(completion_events) == started
    assert await anext(completion_events) == completed
    with pytest.raises(ValueError, match="continuation completion"):
        await anext(completion_events)


@pytest.mark.asyncio
async def test_continuation_completion_rejects_mismatched_batch_linkage() -> None:
    session = _Session({RealtimeCapability.CONTINUATION})
    session.event_values = [
        ResponseStarted(
            response_id="next-response",
            turn_id="turn",
            continuation_of_batch_id="batch",
        ),
        ResponseCompleted(
            response_id="next-response",
            turn_id="turn",
            continuation_of_batch_id="other-batch",
        ),
    ]

    await session.continue_response("batch")
    events = session.events()
    assert await anext(events) == session.event_values[0]
    with pytest.raises(ValueError, match="completion.*batch"):
        await anext(events)


@pytest.mark.asyncio
async def test_unadvertised_continuation_rejects_linked_response() -> None:
    session = _Session()
    session.event_values = [
        ResponseStarted(
            response_id="response",
            turn_id="turn",
            continuation_of_batch_id="batch",
        )
    ]

    with pytest.raises(ValueError, match="continuation capability"):
        await anext(session.events())


@pytest.mark.asyncio
async def test_linked_start_does_not_mask_concurrent_continuation_write_failure() -> None:
    session = _FailingContinuationSession()
    session.event_values = [
        ResponseStarted(
            response_id="next-response",
            turn_id="turn",
            continuation_of_batch_id="batch",
        )
    ]

    continuing = asyncio.create_task(session.continue_response("batch"))
    await session.continue_entered.wait()
    assert await anext(session.events()) == session.event_values[0]
    session.release_continue.set()

    with pytest.raises(RuntimeError, match="continuation write failed"):
        await continuing
    assert "batch" not in session._pending_continuation_batch_ids
    assert "batch" not in session._continuation_responses.values()


def test_provider_api_version_reflects_events_implementation_migration() -> None:
    assert realtime_voice_provider.REALTIME_VOICE_PROVIDER_API_VERSION == 2


@pytest.mark.asyncio
async def test_optional_operations_fail_with_typed_capability_error() -> None:
    session = _Session()
    operations = [
        (session.commit_audio(), RealtimeCapability.INPUT_COMMIT_EVENTS),
        (session.interrupt(), RealtimeCapability.EXPLICIT_INTERRUPTION),
        (
            session.truncate_output("response", "item"),
            RealtimeCapability.OUTPUT_TRUNCATION,
        ),
        (
            session.cancel_tool_call("call", "batch"),
            RealtimeCapability.TOOL_CALL_CANCELLATION,
        ),
        (session.resume_session("session"), RealtimeCapability.SESSION_RESUMPTION),
        (session.update_context("instructions"), RealtimeCapability.DYNAMIC_CONTEXT),
        (session.continue_response("batch"), RealtimeCapability.CONTINUATION),
    ]

    for operation, capability in operations:
        with pytest.raises(UnsupportedRealtimeCapability) as exc:
            await operation
        assert exc.value.capability is capability


@pytest.mark.parametrize(
    "capability",
    [
        RealtimeCapability.EXPLICIT_RESPONSE,
        RealtimeCapability.EXPLICIT_INTERRUPTION,
        RealtimeCapability.OUTPUT_TRUNCATION,
        RealtimeCapability.INPUT_COMMIT_EVENTS,
        RealtimeCapability.TOOL_CALL_CANCELLATION,
        RealtimeCapability.DYNAMIC_CONTEXT,
        RealtimeCapability.SESSION_RESUMPTION,
        RealtimeCapability.CONTINUATION,
    ],
)
def test_operational_capability_requires_subclass_hook_override(capability) -> None:
    with pytest.raises(ValueError, match=rf"{capability.value}.*override"):
        _NoOptionalHooksSession({capability})


def test_passive_capabilities_do_not_require_hooks() -> None:
    session = _NoOptionalHooksSession(
        {
            RealtimeCapability.RESPONSE_METADATA_ECHO,
            RealtimeCapability.INPUT_TRANSCRIPTION,
            RealtimeCapability.OUTPUT_TRANSCRIPTION,
            RealtimeCapability.TOOL_CALLING,
        }
    )

    assert RealtimeCapability.TOOL_CALLING in session.capabilities


@pytest.mark.asyncio
async def test_close_is_idempotent_under_concurrent_callers_and_context_exit() -> None:
    session = _Session()

    async with session:
        await asyncio.gather(session.close(), session.close(), session.close())

    assert session.closed_count == 1


@pytest.mark.asyncio
async def test_cancelled_close_caller_leaves_cleanup_owned_for_second_close() -> None:
    session = _ControlledCloseSession()
    first = asyncio.create_task(session.close())
    await session.close_entered.wait()

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.create_task(session.close())
    session.release_close.set()
    await second
    await session.close()

    assert session.closed_count == 1
    assert session.completed_close_count == 1


@pytest.mark.asyncio
async def test_cancel_racing_success_does_not_repeat_cleanup() -> None:
    session = _ControlledCloseSession()
    first = asyncio.create_task(session.close())
    await session.close_entered.wait()

    session.release_close.set()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    await session.close()

    assert session.closed_count == 1
    assert session.completed_close_count == 1


@pytest.mark.asyncio
async def test_cancelled_waiter_cleanup_failure_is_observed_and_retryable() -> None:
    session = _ControlledCloseSession([RuntimeError("cleanup failed"), None])
    loop = asyncio.get_running_loop()
    orphaned = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: orphaned.append(context))
    try:
        first = asyncio.create_task(session.close())
        await session.close_entered.wait()

        session.release_close.set()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.sleep(0)

        await session.close()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert session.closed_count == 2
    assert session.completed_close_count == 1
    assert orphaned == []


@pytest.mark.asyncio
async def test_close_exception_allows_later_retry() -> None:
    session = _ControlledCloseSession([RuntimeError("cleanup failed"), None])
    session.release_close.set()

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await session.close()

    await session.close()
    await session.close()

    assert session.closed_count == 2
    assert session.completed_close_count == 1
