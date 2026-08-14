"""A1.4 canonical execution and durable provider-output bridge."""

from __future__ import annotations

import asyncio
import copy
import threading
from types import SimpleNamespace

import pytest

from agent.external_tool_batch import get_external_tool_batch_marker
from gateway.session import build_session_key
from tests.gateway.test_realtime_voice_invocation import (
    _committed_execution_attachment,
    _execution_runner,
    _install_permit_tool,
    _source,
)


def _mint(attachment, index: int, *, tool_name: str = "permitted_tool"):
    return attachment.mint_tool_call_permit(
        response_id=f"response-{index}",
        item_id=f"item-{index}",
        call_id=f"call-{index}",
        batch_id="batch-1",
        tool_name=tool_name,
        arguments={"index": index},
    )


def _install_canonical_executor(
    runner,
    source,
    outputs,
    *,
    entered: threading.Event | None = None,
    release: threading.Event | None = None,
    notify_approval: bool = False,
    fail_final_readback: bool = False,
    omit_results: bool = False,
):
    agent = runner._session_states[build_session_key(source)].turn.agent
    durable_rows = []
    persisted = set()
    effects = []
    executor_calls = []
    read_count = 0

    def read_rows(_session_id):
        nonlocal read_count
        read_count += 1
        if fail_final_readback and read_count >= 3:
            return None
        return copy.deepcopy(durable_rows)

    def flush(messages, _history=None):
        for message in messages:
            identity = id(message)
            if identity not in persisted:
                persisted.add(identity)
                durable_rows.append({"id": len(durable_rows) + 1, **copy.deepcopy(message)})
        return True

    def execute(assistant, messages, task_id, api_call_count=0):
        executor_calls.append((assistant, task_id, api_call_count))
        if entered is not None:
            entered.set()
        if release is not None:
            assert release.wait(5)
        if notify_approval:
            from tools.approval import _gateway_notify_for_owner, get_current_session_key

            notifier = _gateway_notify_for_owner(get_current_session_key())
            assert notifier is not None
            notifier({"command": "mutate", "description": "mutating fake"})
        marker = get_external_tool_batch_marker()
        for call in assistant.tool_calls:
            effects.append(call.id)
            if omit_results:
                continue
            messages.append(
                {
                    "role": "tool",
                    "name": call.function.name,
                    "tool_name": call.function.name,
                    "tool_call_id": call.id,
                    "content": outputs[call.id],
                    "display_metadata": {"external_tool_batch_marker": marker},
                }
            )
        flush(messages)

    agent._session_db = SimpleNamespace(get_messages=read_rows)
    agent._flush_messages_to_session_db = flush
    agent._execute_tool_calls = execute
    agent._incremental_persistence_failed = False
    return SimpleNamespace(
        agent=agent,
        durable_rows=durable_rows,
        effects=effects,
        executor_calls=executor_calls,
    )


@pytest.mark.asyncio
async def test_execute_tool_batch_returns_exact_durable_outputs_and_opaque_receipts(
    monkeypatch,
):
    import gateway.run as gateway_run

    source = _source()
    runner, _entry = _execution_runner(source)
    _install_permit_tool(runner, source)
    attachment = await _committed_execution_attachment(runner, source)
    permits = (_mint(attachment, 1), _mint(attachment, 2))
    canonical = _install_canonical_executor(
        runner,
        source,
        {"call-1": "read-only result", "call-2": "delegated/background result"},
        notify_approval=True,
    )
    approval_notices = []
    monkeypatch.setattr(
        gateway_run,
        "_approval_notify_sync",
        lambda data, **_kwargs: approval_notices.append(data),
    )

    results = await attachment.execute_tool_batch(permits)

    assert results == (
        {
            "call_id": "call-1",
            "output": "read-only result",
            "receipt_id": results[0]["receipt_id"],
        },
        {
            "call_id": "call-2",
            "output": "delegated/background result",
            "receipt_id": results[1]["receipt_id"],
        },
    )
    assert all(type(result) is dict and set(result) == {"call_id", "output", "receipt_id"} for result in results)
    assert all(type(result["receipt_id"]) is str and result["receipt_id"] for result in results)
    assert len({result["receipt_id"] for result in results}) == 2
    assert not ({result["receipt_id"] for result in results} & {"durable-session-1", "1", "2", "3"})
    assert canonical.effects == ["call-1", "call-2"]
    assert len(canonical.executor_calls) == 1
    assert approval_notices == [{"command": "mutate", "description": "mutating fake"}]


@pytest.mark.asyncio
async def test_execute_tool_batch_replay_has_zero_second_effects():
    from gateway.realtime_voice_invocation import RealtimeVoiceInvocationError

    source = _source()
    runner, _entry = _execution_runner(source)
    _install_permit_tool(runner, source)
    attachment = await _committed_execution_attachment(runner, source)
    permit = _mint(attachment, 1)
    canonical = _install_canonical_executor(runner, source, {"call-1": "once"})

    await attachment.execute_tool_batch((permit,))
    with pytest.raises(RealtimeVoiceInvocationError):
        await attachment.execute_tool_batch((permit,))

    assert canonical.effects == ["call-1"]
    assert len(canonical.executor_calls) == 1


@pytest.mark.asyncio
async def test_execute_tool_batch_rejects_mixed_or_closed_before_admission_atomically():
    from gateway.realtime_voice_invocation import (
        RealtimeVoiceInvocationError,
        _record_for_realtime_tool_call_permit,
    )

    source = _source()
    runner, _entry = _execution_runner(source)
    _install_permit_tool(runner, source)
    first = await _committed_execution_attachment(runner, source)
    second = await _committed_execution_attachment(runner, source)
    first_permit = _mint(first, 1)
    second_permit = _mint(second, 2)
    canonical = _install_canonical_executor(
        runner, source, {"call-1": "one", "call-2": "two"}
    )

    with pytest.raises(RealtimeVoiceInvocationError):
        await first.execute_tool_batch((first_permit, first_permit))
    assert _record_for_realtime_tool_call_permit(first, first_permit).consumed is False

    with pytest.raises(RealtimeVoiceInvocationError):
        await first.execute_tool_batch((first_permit, second_permit))
    assert _record_for_realtime_tool_call_permit(first, first_permit).consumed is False
    assert _record_for_realtime_tool_call_permit(second, second_permit).consumed is False
    assert canonical.effects == []

    first.close()
    with pytest.raises(RealtimeVoiceInvocationError):
        await first.execute_tool_batch((first_permit,))
    assert canonical.effects == []


@pytest.mark.asyncio
async def test_close_after_atomic_admission_does_not_cancel_accepted_execution():
    source = _source()
    runner, _entry = _execution_runner(source)
    _install_permit_tool(runner, source)
    attachment = await _committed_execution_attachment(runner, source)
    permit = _mint(attachment, 1)
    entered = threading.Event()
    release = threading.Event()
    canonical = _install_canonical_executor(
        runner,
        source,
        {"call-1": "accepted"},
        entered=entered,
        release=release,
    )

    pending = asyncio.create_task(attachment.execute_tool_batch((permit,)))
    assert await asyncio.to_thread(entered.wait, 5)
    attachment.close()
    release.set()

    assert await pending == (
        {
            "call_id": "call-1",
            "output": "accepted",
            "receipt_id": (await pending)[0]["receipt_id"],
        },
    )
    assert canonical.effects == ["call-1"]


@pytest.mark.asyncio
async def test_durable_readback_failure_mints_no_provider_receipt(monkeypatch):
    import gateway.realtime_voice_invocation as invocation_module
    from agent.external_tool_batch import ExternalToolBatchPersistenceError

    source = _source()
    runner, _entry = _execution_runner(source)
    _install_permit_tool(runner, source)
    attachment = await _committed_execution_attachment(runner, source)
    permit = _mint(attachment, 1)
    canonical = _install_canonical_executor(
        runner,
        source,
        {"call-1": "persisted but unreadable"},
        fail_final_readback=True,
    )
    minted = []
    monkeypatch.setattr(
        invocation_module,
        "_mint_realtime_execution_receipt_id",
        lambda: minted.append("receipt") or "receipt",
        raising=False,
    )

    with pytest.raises(ExternalToolBatchPersistenceError):
        await attachment.execute_tool_batch((permit,))

    assert canonical.effects == ["call-1"]
    assert minted == []


@pytest.mark.asyncio
async def test_execution_failure_does_not_expose_private_call_id():
    from agent.external_tool_batch import ExternalToolBatchPersistenceError

    source = _source()
    runner, _entry = _execution_runner(source)
    _install_permit_tool(runner, source)
    attachment = await _committed_execution_attachment(runner, source)
    permit = attachment.mint_tool_call_permit(
        response_id="response-safe",
        item_id="item-safe",
        call_id="private-call-id",
        batch_id="batch-safe",
        tool_name="permitted_tool",
        arguments={},
    )
    _install_canonical_executor(
        runner,
        source,
        {"private-call-id": "unused"},
        omit_results=True,
    )

    with pytest.raises(ExternalToolBatchPersistenceError) as exc_info:
        await attachment.execute_tool_batch((permit,))
    assert "private-call-id" not in str(exc_info.value)


def test_provider_ready_output_honors_small_byte_limit(monkeypatch):
    from gateway.realtime_voice_invocation import _provider_ready_tool_output

    monkeypatch.setattr("tools.tool_output_limits.get_max_bytes", lambda: 8)
    output = _provider_ready_tool_output("abcdefghijk")

    assert len(output.encode("utf-8")) <= 8


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permits",
    [(), [], [object()], tuple(object() for _ in range(129))],
)
async def test_execute_tool_batch_requires_exact_nonempty_tuple(permits):
    from gateway.realtime_voice_invocation import RealtimeVoiceInvocationError

    source = _source()
    runner, _entry = _execution_runner(source)
    _install_permit_tool(runner, source)
    attachment = await _committed_execution_attachment(runner, source)
    canonical = _install_canonical_executor(runner, source, {})

    with pytest.raises(RealtimeVoiceInvocationError):
        await attachment.execute_tool_batch(permits)
    assert canonical.effects == []
