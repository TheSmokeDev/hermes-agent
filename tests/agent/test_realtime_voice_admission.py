from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from agent.realtime_voice_admission import (
    AdmissionStatus,
    FinalTranscriptAdmission,
    RealtimeSessionBinding,
    RealtimeUtterance,
)
from agent.realtime_voice_provider import (
    MAX_IDENTIFIER_LENGTH,
    InputTranscript,
    SessionReady,
    TranscriptProvenance,
    TranscriptRole,
)


@dataclass(frozen=True)
class _Permit:
    value: str


@dataclass(frozen=True)
class _Receipt:
    value: str


class _Authorizer:
    def __init__(self, permit: _Permit | None = None) -> None:
        self.permit = permit
        self.calls: list[tuple[RealtimeSessionBinding, RealtimeUtterance]] = []
        self.revoked: list[_Permit] = []

    async def authorize(
        self, binding: RealtimeSessionBinding, utterance: RealtimeUtterance
    ) -> _Permit | None:
        self.calls.append((binding, utterance))
        return self.permit

    async def revoke(self, permit: _Permit) -> None:
        self.revoked.append(permit)


class _Ingress:
    def __init__(self, receipt: _Receipt) -> None:
        self.receipt = receipt
        self.calls: list[
            tuple[RealtimeSessionBinding, RealtimeUtterance, _Permit]
        ] = []

    async def submit(
        self,
        binding: RealtimeSessionBinding,
        utterance: RealtimeUtterance,
        permit: _Permit,
    ) -> _Receipt:
        self.calls.append((binding, utterance, permit))
        return self.receipt


class _ControlledAuthorizer(_Authorizer):
    def __init__(self, permit: _Permit) -> None:
        super().__init__(permit)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def authorize(
        self, binding: RealtimeSessionBinding, utterance: RealtimeUtterance
    ) -> _Permit | None:
        self.calls.append((binding, utterance))
        self.entered.set()
        await self.release.wait()
        return self.permit


class _AtomicHost:
    def __init__(self, permit: _Permit, receipt: _Receipt) -> None:
        self.permit = permit
        self.receipt = receipt
        self.state = "new"
        self.revoke_calls = 0
        self.submit_calls = 0
        self.submit_entered = asyncio.Event()
        self.release_submit = asyncio.Event()

    async def authorize(
        self, binding: RealtimeSessionBinding, utterance: RealtimeUtterance
    ) -> _Permit:
        assert self.state == "new"
        self.state = "issued"
        return self.permit

    async def revoke(self, permit: _Permit) -> None:
        assert permit is self.permit
        self.revoke_calls += 1
        if self.state == "issued":
            self.state = "revoked"

    async def submit(
        self,
        binding: RealtimeSessionBinding,
        utterance: RealtimeUtterance,
        permit: _Permit,
    ) -> _Receipt:
        assert permit is self.permit
        self.submit_calls += 1
        self.submit_entered.set()
        await self.release_submit.wait()
        if self.state != "issued":
            raise RuntimeError("permit revoked")
        self.state = "consumed"
        return self.receipt


class _ControlledRevokeHost(_AtomicHost):
    def __init__(self, permit: _Permit, receipt: _Receipt) -> None:
        super().__init__(permit, receipt)
        self.revoke_entered = asyncio.Event()
        self.release_revoke = asyncio.Event()
        self.revoke_completed = 0

    async def revoke(self, permit: _Permit) -> None:
        assert permit is self.permit
        self.revoke_calls += 1
        self.revoke_entered.set()
        await self.release_revoke.wait()
        self.state = "revoked"
        self.revoke_completed += 1


class _RetryRevokeHost(_AtomicHost):
    async def revoke(self, permit: _Permit) -> None:
        assert permit is self.permit
        self.revoke_calls += 1
        if self.revoke_calls == 1:
            raise RuntimeError("revocation failed")
        self.state = "revoked"


class _RetryAuthorizer(_Authorizer):
    def __init__(self, permit: _Permit) -> None:
        super().__init__(permit)
        self.revoke_attempts = 0

    async def revoke(self, permit: _Permit) -> None:
        assert permit is self.permit
        self.revoke_attempts += 1
        if self.revoke_attempts == 1:
            raise RuntimeError("revocation failed")
        self.revoked.append(permit)


class _CancelledRevokeHost(_AtomicHost):
    async def revoke(self, permit: _Permit) -> None:
        assert permit is self.permit
        self.revoke_calls += 1
        if self.revoke_calls == 1:
            raise asyncio.CancelledError
        self.state = "revoked"


class _ReusablePermitHost:
    def __init__(self, permit: _Permit, receipt: _Receipt) -> None:
        self.permit = permit
        self.receipt = receipt
        self.consumed = False
        self.authorize_calls = 0
        self.submit_calls = 0
        self.revoke_calls = 0

    async def authorize(
        self, binding: RealtimeSessionBinding, utterance: RealtimeUtterance
    ) -> _Permit:
        self.authorize_calls += 1
        return self.permit

    async def revoke(self, permit: _Permit) -> None:
        assert permit is self.permit
        self.revoke_calls += 1

    async def submit(
        self,
        binding: RealtimeSessionBinding,
        utterance: RealtimeUtterance,
        permit: _Permit,
    ) -> _Receipt:
        assert permit is self.permit
        self.submit_calls += 1
        if self.consumed:
            raise RuntimeError("permit already consumed")
        self.consumed = True
        return self.receipt


class _DriftRejectingIngress(_Ingress):
    async def submit(
        self,
        binding: RealtimeSessionBinding,
        utterance: RealtimeUtterance,
        permit: _Permit,
    ) -> _Receipt:
        self.calls.append((binding, utterance, permit))
        raise RuntimeError("binding drift")


class _MultiPermitHost:
    def __init__(self) -> None:
        self.permits: list[_Permit] = []
        self.states: dict[str, str] = {}
        self.submit_count = 0
        self.all_submits_entered = asyncio.Event()
        self.release_submits = asyncio.Event()
        self.first_revoke_failed = asyncio.Event()
        self.second_revoke_entered = asyncio.Event()
        self.release_second_revoke = asyncio.Event()
        self.failed_once = False

    async def authorize(
        self, binding: RealtimeSessionBinding, utterance: RealtimeUtterance
    ) -> _Permit:
        permit = _Permit(f"permit-{len(self.permits) + 1}")
        self.permits.append(permit)
        self.states[permit.value] = "issued"
        return permit

    async def submit(
        self,
        binding: RealtimeSessionBinding,
        utterance: RealtimeUtterance,
        permit: _Permit,
    ) -> _Receipt:
        self.submit_count += 1
        if self.submit_count == 2:
            self.all_submits_entered.set()
        await self.release_submits.wait()
        if self.states[permit.value] != "issued":
            raise RuntimeError("permit revoked")
        self.states[permit.value] = "consumed"
        return _Receipt(permit.value)

    async def revoke(self, permit: _Permit) -> None:
        if permit is self.permits[0] and not self.failed_once:
            self.failed_once = True
            self.first_revoke_failed.set()
            raise RuntimeError("first revoke failed")
        if permit is self.permits[1] and self.states[permit.value] == "issued":
            self.second_revoke_entered.set()
            await self.release_second_revoke.wait()
        self.states[permit.value] = "revoked"


def _binding(provider_session_id: str = "provider-session") -> RealtimeSessionBinding:
    return RealtimeSessionBinding(
        profile_id="default",
        routing_key="discord:channel:user",
        runtime_session_id="runtime-session",
        durable_session_id="durable-session",
        provider_session_id=provider_session_id,
        selection_generation=7,
    )


def _operator_transcript(
    *,
    final: bool,
    text: str = "  check the date  ",
    item_id: str = "item-1",
    turn_id: str = "provider-turn-1",
) -> InputTranscript:
    return InputTranscript(
        item_id=item_id,
        turn_id=turn_id,
        text=text,
        final=final,
        role=TranscriptRole.OPERATOR,
        provenance=TranscriptProvenance.OPERATOR_INPUT,
    )


@pytest.mark.asyncio
async def test_partial_operator_transcript_is_display_only_and_does_not_reserve_final_key() -> None:
    permit = _Permit("permit-1")
    receipt = _Receipt("receipt-1")
    authorizer = _Authorizer(permit)
    ingress = _Ingress(receipt)
    admission = FinalTranscriptAdmission(
        _binding(), authorizer, ingress, clock=lambda: 42.5
    )

    partial_result = await admission.admit(_operator_transcript(final=False))
    final_result = await admission.admit(_operator_transcript(final=True))

    assert partial_result.status is AdmissionStatus.IGNORED_PARTIAL
    assert partial_result.receipt is None
    assert final_result.status is AdmissionStatus.SUBMITTED
    assert final_result.receipt is receipt
    assert len(authorizer.calls) == 1
    assert len(ingress.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_final", [None, 0, 1, "false"])
async def test_non_boolean_final_is_terminally_rejected_without_authority(
    malformed_final: object,
) -> None:
    authorizer = _Authorizer(_Permit("must-not-issue"))
    ingress = _Ingress(_Receipt("must-not-submit"))
    admission = FinalTranscriptAdmission(
        _binding(), authorizer, ingress, clock=lambda: 42.5
    )
    malformed = InputTranscript(
        item_id="malformed-final-item",
        turn_id="malformed-final-turn",
        text="do not authorize this",
        final=malformed_final,  # type: ignore[arg-type]
        role=TranscriptRole.OPERATOR,
        provenance=TranscriptProvenance.OPERATOR_INPUT,
    )
    valid_replay = InputTranscript(
        item_id="malformed-final-item",
        turn_id="malformed-final-turn",
        text="attempt to upgrade the same identity",
        final=True,
        role=TranscriptRole.OPERATOR,
        provenance=TranscriptProvenance.OPERATOR_INPUT,
    )

    first = await admission.admit(malformed)
    replay = await admission.admit(valid_replay)

    assert first.status is AdmissionStatus.REJECTED
    assert replay.status is AdmissionStatus.DUPLICATE
    assert authorizer.calls == []
    assert ingress.calls == []


@pytest.mark.asyncio
async def test_authorized_final_operator_submits_exact_utterance_once() -> None:
    binding = _binding()
    permit = _Permit("permit-1")
    receipt = _Receipt("receipt-1")
    authorizer = _Authorizer(permit)
    ingress = _Ingress(receipt)
    admission = FinalTranscriptAdmission(
        binding, authorizer, ingress, clock=lambda: 123.25
    )

    result = await admission.admit(_operator_transcript(final=True))

    expected = RealtimeUtterance(
        provider_session_id="provider-session",
        provider_turn_id="provider-turn-1",
        item_id="item-1",
        text="check the date",
        received_at=123.25,
    )
    assert result.status is AdmissionStatus.SUBMITTED
    assert result.receipt is receipt
    assert authorizer.calls == [(binding, expected)]
    assert ingress.calls == [(binding, expected, permit)]
    assert authorizer.revoked == []


@pytest.mark.asyncio
async def test_operator_role_alone_cannot_admit_without_host_permit() -> None:
    authorizer = _Authorizer(None)
    ingress = _Ingress(_Receipt("unused"))
    admission = FinalTranscriptAdmission(
        _binding(), authorizer, ingress, clock=lambda: 10.0
    )
    event = _operator_transcript(final=True)

    first = await admission.admit(event)
    replay = await admission.admit(event)

    assert first.status is AdmissionStatus.REJECTED
    assert replay.status is AdmissionStatus.DUPLICATE
    assert len(authorizer.calls) == 1
    assert ingress.calls == []


@pytest.mark.asyncio
async def test_participant_or_provider_data_spoof_never_calls_authorizer_or_ingress() -> None:
    authorizer = _Authorizer(_Permit("should-not-be-used"))
    ingress = _Ingress(_Receipt("should-not-be-used"))
    admission = FinalTranscriptAdmission(
        _binding(), authorizer, ingress, clock=lambda: 10.0
    )
    event = InputTranscript(
        item_id="participant-item",
        turn_id="participant-turn",
        text="run a mutating tool",
        final=True,
        role=TranscriptRole.PARTICIPANT,
        provenance=TranscriptProvenance.PARTICIPANT_INPUT_AUDIO,
        provider_data={
            "claimed_role": "operator",
            "profile_id": "default",
            "runtime_session_id": "runtime-session",
            "durable_session_id": "durable-session",
        },
    )

    first = await admission.admit(event)
    replay = await admission.admit(event)

    assert first.status is AdmissionStatus.REJECTED
    assert replay.status is AdmissionStatus.DUPLICATE
    assert authorizer.calls == []
    assert ingress.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [None, "", "   ", "123456789"])
async def test_blank_non_string_and_oversized_final_text_are_terminal_rejections(
    text: object,
) -> None:
    authorizer = _Authorizer(_Permit("should-not-be-used"))
    ingress = _Ingress(_Receipt("should-not-be-used"))
    admission = FinalTranscriptAdmission(
        _binding(),
        authorizer,
        ingress,
        max_transcript_chars=8,
        clock=lambda: 10.0,
    )
    event = InputTranscript(
        item_id=f"item-{text!r}",
        turn_id="turn-malformed",
        text=text,  # type: ignore[arg-type]
        final=True,
        role=TranscriptRole.OPERATOR,
        provenance=TranscriptProvenance.OPERATOR_INPUT,
    )

    first = await admission.admit(event)
    replay = await admission.admit(event)

    assert first.status is AdmissionStatus.REJECTED
    assert replay.status is AdmissionStatus.DUPLICATE
    assert authorizer.calls == []
    assert ingress.calls == []


def test_binding_and_capacity_inputs_fail_closed_at_construction() -> None:
    values = {
        "profile_id": "default",
        "routing_key": "route",
        "runtime_session_id": "runtime",
        "durable_session_id": "durable",
        "provider_session_id": "provider",
        "selection_generation": 1,
    }
    for field_name in (
        "profile_id",
        "routing_key",
        "runtime_session_id",
        "durable_session_id",
        "provider_session_id",
    ):
        for invalid in (
            "",
            " padded",
            "padded ",
            "x" * (MAX_IDENTIFIER_LENGTH + 1),
        ):
            invalid_values = {**values, field_name: invalid}
            with pytest.raises(ValueError, match=field_name):
                RealtimeSessionBinding(**invalid_values)

    for generation in (0, -1, True, 1.5, "1"):
        with pytest.raises((TypeError, ValueError), match="selection_generation"):
            RealtimeSessionBinding(**{**values, "selection_generation": generation})

    binding = RealtimeSessionBinding(**values)
    authorizer = _Authorizer(None)
    ingress = _Ingress(_Receipt("unused"))
    for field_name in ("replay_capacity", "max_transcript_chars"):
        for invalid in (0, -1, True, 1.5, "1"):
            kwargs = {field_name: invalid}
            with pytest.raises((TypeError, ValueError), match=field_name):
                FinalTranscriptAdmission(
                    binding,
                    authorizer,
                    ingress,
                    clock=lambda: 1.0,
                    **kwargs,
                )


@pytest.mark.parametrize(
    ("field_name", "invalid"),
    [
        ("provider_session_id", ""),
        ("provider_turn_id", " padded"),
        ("item_id", "x" * (MAX_IDENTIFIER_LENGTH + 1)),
        ("text", ""),
        ("text", " padded "),
        ("text", None),
        ("received_at", float("inf")),
        ("received_at", True),
    ],
)
def test_realtime_utterance_rejects_malformed_direct_construction(
    field_name: str, invalid: object
) -> None:
    values: dict[str, object] = {
        "provider_session_id": "provider",
        "provider_turn_id": "turn",
        "item_id": "item",
        "text": "accepted text",
        "received_at": 1.0,
    }

    with pytest.raises((TypeError, ValueError)):
        RealtimeUtterance(**(values | {field_name: invalid}))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_replay_capacity_fails_closed_without_evicting_old_identities() -> None:
    authorizer = _Authorizer(None)
    ingress = _Ingress(_Receipt("unused"))
    admission = FinalTranscriptAdmission(
        _binding(), authorizer, ingress, replay_capacity=2, clock=lambda: 1.0
    )
    first = _operator_transcript(final=True, item_id="item-1", turn_id="turn-1")
    second = _operator_transcript(final=True, item_id="item-2", turn_id="turn-2")
    third = _operator_transcript(final=True, item_id="item-3", turn_id="turn-3")
    fourth = _operator_transcript(final=True, item_id="item-4", turn_id="turn-4")

    assert (await admission.admit(first)).status is AdmissionStatus.REJECTED
    assert (await admission.admit(second)).status is AdmissionStatus.REJECTED
    assert (
        await admission.admit(third)
    ).status is AdmissionStatus.CAPACITY_EXHAUSTED
    assert (await admission.admit(first)).status is AdmissionStatus.DUPLICATE
    assert (
        await admission.admit(fourth)
    ).status is AdmissionStatus.CAPACITY_EXHAUSTED
    assert len(authorizer.calls) == 2
    assert ingress.calls == []


@pytest.mark.asyncio
async def test_close_while_authorize_is_pending_revokes_late_permit_and_never_submits() -> None:
    permit = _Permit("late")
    authorizer = _ControlledAuthorizer(permit)
    ingress = _Ingress(_Receipt("unused"))
    admission = FinalTranscriptAdmission(
        _binding(), authorizer, ingress, clock=lambda: 1.0
    )
    admit_task = asyncio.create_task(
        admission.admit(_operator_transcript(final=True))
    )
    await authorizer.entered.wait()

    await admission.close()
    await admission.close()
    authorizer.release.set()
    result = await admit_task

    assert result.status is AdmissionStatus.CLOSED
    assert authorizer.revoked == [permit]
    assert ingress.calls == []


@pytest.mark.asyncio
async def test_close_racing_submit_obeys_atomic_consume_or_revoke_linearization() -> None:
    permit = _Permit("permit")
    host = _AtomicHost(permit, _Receipt("receipt"))
    admission = FinalTranscriptAdmission(
        _binding(), host, host, clock=lambda: 1.0
    )
    admit_task = asyncio.create_task(
        admission.admit(_operator_transcript(final=True))
    )
    await host.submit_entered.wait()

    await admission.close()
    host.release_submit.set()

    with pytest.raises(RuntimeError, match="permit revoked"):
        await admit_task
    assert host.state == "revoked"
    assert host.revoke_calls == 1
    assert host.submit_calls == 1
    assert (
        await admission.admit(_operator_transcript(final=True))
    ).status is AdmissionStatus.CLOSED


@pytest.mark.asyncio
async def test_submit_cancellation_finishes_revocation_before_cancellation_escapes() -> None:
    permit = _Permit("permit")
    host = _ControlledRevokeHost(permit, _Receipt("receipt"))
    admission = FinalTranscriptAdmission(
        _binding(), host, host, clock=lambda: 1.0
    )
    admit_task = asyncio.create_task(
        admission.admit(_operator_transcript(final=True))
    )
    await host.submit_entered.wait()

    admit_task.cancel()
    await host.revoke_entered.wait()
    admit_task.cancel()
    host.release_revoke.set()

    with pytest.raises(asyncio.CancelledError):
        await admit_task
    assert host.revoke_calls == 1
    assert host.revoke_completed == 1
    assert host.state == "revoked"
    assert (
        await admission.admit(_operator_transcript(final=True))
    ).status is AdmissionStatus.DUPLICATE


@pytest.mark.asyncio
async def test_non_input_events_never_reach_authorizer_or_ingress() -> None:
    authorizer = _Authorizer(_Permit("unused"))
    ingress = _Ingress(_Receipt("unused"))
    admission = FinalTranscriptAdmission(
        _binding(), authorizer, ingress, clock=lambda: 1.0
    )

    result = await admission.admit(SessionReady(session_id="provider-session"))

    assert result.status is AdmissionStatus.REJECTED
    assert authorizer.calls == []
    assert ingress.calls == []


@pytest.mark.asyncio
async def test_duplicate_key_with_changed_text_is_still_duplicate() -> None:
    authorizer = _Authorizer(None)
    ingress = _Ingress(_Receipt("unused"))
    admission = FinalTranscriptAdmission(
        _binding(), authorizer, ingress, clock=lambda: 1.0
    )

    first = await admission.admit(
        _operator_transcript(final=True, text="first wording")
    )
    changed = await admission.admit(
        _operator_transcript(final=True, text="mutating rewritten wording")
    )

    assert first.status is AdmissionStatus.REJECTED
    assert changed.status is AdmissionStatus.DUPLICATE
    assert len(authorizer.calls) == 1
    assert ingress.calls == []


@pytest.mark.asyncio
async def test_concurrent_duplicate_finals_authorize_and_submit_once() -> None:
    permit = _Permit("permit")
    authorizer = _ControlledAuthorizer(permit)
    ingress = _Ingress(_Receipt("receipt"))
    admission = FinalTranscriptAdmission(
        _binding(), authorizer, ingress, clock=lambda: 1.0
    )
    event = _operator_transcript(final=True)

    first_task = asyncio.create_task(admission.admit(event))
    await authorizer.entered.wait()
    duplicate = await admission.admit(event)
    authorizer.release.set()
    first = await first_task

    assert first.status is AdmissionStatus.SUBMITTED
    assert duplicate.status is AdmissionStatus.DUPLICATE
    assert len(authorizer.calls) == 1
    assert len(ingress.calls) == 1


@pytest.mark.asyncio
async def test_authorizer_exception_and_cancellation_leave_identity_terminal() -> None:
    class FailingAuthorizer(_Authorizer):
        async def authorize(
            self, binding: RealtimeSessionBinding, utterance: RealtimeUtterance
        ) -> _Permit | None:
            self.calls.append((binding, utterance))
            raise RuntimeError("authorization failed")

    failing = FailingAuthorizer(None)
    ingress = _Ingress(_Receipt("unused"))
    failed_admission = FinalTranscriptAdmission(
        _binding(), failing, ingress, clock=lambda: 1.0
    )
    event = _operator_transcript(final=True)

    with pytest.raises(RuntimeError, match="authorization failed"):
        await failed_admission.admit(event)
    assert (await failed_admission.admit(event)).status is AdmissionStatus.DUPLICATE

    controlled = _ControlledAuthorizer(_Permit("late"))
    cancelled_admission = FinalTranscriptAdmission(
        _binding(), controlled, ingress, clock=lambda: 1.0
    )
    cancelled_task = asyncio.create_task(cancelled_admission.admit(event))
    await controlled.entered.wait()
    cancelled_task.cancel()
    controlled.release.set()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_task
    assert (
        await cancelled_admission.admit(event)
    ).status is AdmissionStatus.DUPLICATE
    assert ingress.calls == []


@pytest.mark.asyncio
async def test_cancellation_after_permit_issue_revokes_before_propagating() -> None:
    permit = _Permit("issued-before-cancel")
    authorizer = _ControlledAuthorizer(permit)
    ingress = _Ingress(_Receipt("must-not-submit"))
    admission = FinalTranscriptAdmission(
        _binding(), authorizer, ingress, clock=lambda: 1.0
    )
    event = _operator_transcript(final=True)
    admit_task = asyncio.create_task(admission.admit(event))
    await authorizer.entered.wait()

    admit_task.cancel()
    authorizer.release.set()

    with pytest.raises(asyncio.CancelledError):
        await admit_task
    assert authorizer.revoked == [permit]
    assert ingress.calls == []
    assert (await admission.admit(event)).status is AdmissionStatus.DUPLICATE


@pytest.mark.asyncio
async def test_same_turn_and_item_in_distinct_provider_sessions_are_distinct() -> None:
    first_authorizer = _Authorizer(_Permit("first"))
    second_authorizer = _Authorizer(_Permit("second"))
    first_ingress = _Ingress(_Receipt("first"))
    second_ingress = _Ingress(_Receipt("second"))
    first = FinalTranscriptAdmission(
        _binding("provider-a"), first_authorizer, first_ingress, clock=lambda: 1.0
    )
    second = FinalTranscriptAdmission(
        _binding("provider-b"), second_authorizer, second_ingress, clock=lambda: 1.0
    )
    event = _operator_transcript(final=True)

    assert (await first.admit(event)).status is AdmissionStatus.SUBMITTED
    assert (await second.admit(event)).status is AdmissionStatus.SUBMITTED
    assert first_authorizer.calls[0][1].provider_session_id == "provider-a"
    assert second_authorizer.calls[0][1].provider_session_id == "provider-b"


@pytest.mark.asyncio
async def test_accepted_canonical_work_survives_later_voice_close() -> None:
    permit = _Permit("consumed")
    authorizer = _Authorizer(permit)
    ingress = _Ingress(_Receipt("canonical"))
    admission = FinalTranscriptAdmission(
        _binding(), authorizer, ingress, clock=lambda: 1.0
    )

    result = await admission.admit(_operator_transcript(final=True))
    await admission.close()

    assert result.status is AdmissionStatus.SUBMITTED
    assert result.receipt == _Receipt("canonical")
    assert authorizer.revoked == []


@pytest.mark.asyncio
async def test_submit_exception_is_visible_and_identity_remains_terminal() -> None:
    class FailingIngress(_Ingress):
        async def submit(
            self,
            binding: RealtimeSessionBinding,
            utterance: RealtimeUtterance,
            permit: _Permit,
        ) -> _Receipt:
            self.calls.append((binding, utterance, permit))
            raise RuntimeError("submit failed")

    permit = _Permit("permit")
    authorizer = _Authorizer(permit)
    ingress = FailingIngress(_Receipt("unused"))
    admission = FinalTranscriptAdmission(
        _binding(), authorizer, ingress, clock=lambda: 1.0
    )
    event = _operator_transcript(final=True)

    with pytest.raises(RuntimeError, match="submit failed"):
        await admission.admit(event)
    assert authorizer.revoked == [permit]
    assert (await admission.admit(event)).status is AdmissionStatus.DUPLICATE


@pytest.mark.asyncio
async def test_submit_failure_retains_permit_when_revoke_needs_close_retry() -> None:
    class FailingIngress(_Ingress):
        async def submit(
            self,
            binding: RealtimeSessionBinding,
            utterance: RealtimeUtterance,
            permit: _Permit,
        ) -> _Receipt:
            self.calls.append((binding, utterance, permit))
            raise RuntimeError("submit failed")

    permit = _Permit("permit")
    authorizer = _RetryAuthorizer(permit)
    ingress = FailingIngress(_Receipt("unused"))
    admission = FinalTranscriptAdmission(
        _binding(), authorizer, ingress, clock=lambda: 1.0
    )

    with pytest.raises(RuntimeError, match="revocation failed"):
        await admission.admit(_operator_transcript(final=True))
    assert authorizer.revoke_attempts == 1

    await admission.close()
    assert authorizer.revoke_attempts == 2
    assert authorizer.revoked == [permit]


@pytest.mark.asyncio
async def test_close_revocation_failure_is_visible_and_retryable() -> None:
    permit = _Permit("permit")
    host = _RetryRevokeHost(permit, _Receipt("unused"))
    admission = FinalTranscriptAdmission(
        _binding(), host, host, clock=lambda: 1.0
    )
    admit_task = asyncio.create_task(
        admission.admit(_operator_transcript(final=True))
    )
    await host.submit_entered.wait()

    with pytest.raises(RuntimeError, match="revocation failed"):
        await admission.close()
    assert host.state == "issued"

    await admission.close()
    assert host.state == "revoked"
    assert host.revoke_calls == 2

    host.release_submit.set()
    with pytest.raises(RuntimeError, match="permit revoked"):
        await admit_task


@pytest.mark.asyncio
async def test_cancelled_revocation_is_visible_and_retryable() -> None:
    permit = _Permit("permit")
    host = _CancelledRevokeHost(permit, _Receipt("unused"))
    admission = FinalTranscriptAdmission(
        _binding(), host, host, clock=lambda: 1.0
    )
    admit_task = asyncio.create_task(
        admission.admit(_operator_transcript(final=True))
    )
    await host.submit_entered.wait()

    with pytest.raises(RuntimeError, match="revocation was cancelled"):
        await admission.close()
    assert host.state == "issued"

    await admission.close()
    assert host.state == "revoked"
    assert host.revoke_calls == 2

    host.release_submit.set()
    with pytest.raises(RuntimeError, match="permit revoked"):
        await admit_task


@pytest.mark.asyncio
async def test_reused_opaque_permit_cannot_submit_two_distinct_utterances() -> None:
    host = _ReusablePermitHost(_Permit("same"), _Receipt("first"))
    admission = FinalTranscriptAdmission(
        _binding(), host, host, clock=lambda: 1.0
    )
    first = _operator_transcript(
        final=True, item_id="item-1", turn_id="turn-1"
    )
    second = _operator_transcript(
        final=True, item_id="item-2", turn_id="turn-2"
    )

    assert (await admission.admit(first)).status is AdmissionStatus.SUBMITTED
    with pytest.raises(RuntimeError, match="permit already consumed"):
        await admission.admit(second)

    assert host.authorize_calls == 2
    assert host.submit_calls == 2
    assert host.revoke_calls == 1
    assert (await admission.admit(second)).status is AdmissionStatus.DUPLICATE


@pytest.mark.asyncio
async def test_host_binding_drift_rejection_is_visible_revoked_and_terminal() -> None:
    permit = _Permit("stale")
    authorizer = _Authorizer(permit)
    ingress = _DriftRejectingIngress(_Receipt("unused"))
    admission = FinalTranscriptAdmission(
        _binding(), authorizer, ingress, clock=lambda: 1.0
    )
    event = _operator_transcript(final=True)

    with pytest.raises(RuntimeError, match="binding drift"):
        await admission.admit(event)

    assert authorizer.revoked == [permit]
    assert len(ingress.calls) == 1
    assert (await admission.admit(event)).status is AdmissionStatus.DUPLICATE


@pytest.mark.asyncio
async def test_close_waits_for_all_revocations_before_reporting_one_failure() -> None:
    host = _MultiPermitHost()
    admission = FinalTranscriptAdmission(
        _binding(), host, host, clock=lambda: 1.0
    )
    first_task = asyncio.create_task(
        admission.admit(
            _operator_transcript(final=True, item_id="item-1", turn_id="turn-1")
        )
    )
    second_task = asyncio.create_task(
        admission.admit(
            _operator_transcript(final=True, item_id="item-2", turn_id="turn-2")
        )
    )
    await host.all_submits_entered.wait()

    close_task = asyncio.create_task(admission.close())
    await host.first_revoke_failed.wait()
    await host.second_revoke_entered.wait()
    completed_before_sibling_cleanup = close_task.done()
    host.release_second_revoke.set()

    with pytest.raises(RuntimeError, match="first revoke failed"):
        await close_task
    assert not completed_before_sibling_cleanup
    assert host.states["permit-2"] == "revoked"

    await admission.close()
    assert host.states == {"permit-1": "revoked", "permit-2": "revoked"}

    host.release_submits.set()
    results = await asyncio.gather(first_task, second_task, return_exceptions=True)
    assert all(
        isinstance(result, RuntimeError) and str(result) == "permit revoked"
        for result in results
    )
