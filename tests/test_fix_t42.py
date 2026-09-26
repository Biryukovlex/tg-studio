"""T42 safe-model-error-diagnostics tests (memory repository, scripted adapters)."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from types import SimpleNamespace

import pytest
from pydantic_ai.ui.ag_ui import AGUIAdapter

from app.config import Settings
from app.studio.repository import MemoryStudioRepository
from app.studio.service import StudioService


def _payload(conversation_id, run_id):
    import json as _json

    return _json.dumps({
        "threadId": str(conversation_id),
        "runId": str(run_id),
        "messages": [{"id": "m1", "role": "user", "content": "Summarize the channel."}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }).encode()


class _RunErrorAdapter:
    build_run_input = staticmethod(AGUIAdapter.build_run_input)

    def __init__(self, message: str, **_kwargs):
        self._message = message

    async def run_stream(self, **kwargs):
        from ag_ui.core import RunStartedEvent

        yield RunStartedEvent(thread_id=kwargs["conversation_id"], run_id=kwargs["run_id"])
        yield SimpleNamespace(type="RUN_ERROR", message=self._message)

    def encode_stream(self, stream):
        return stream


async def _failed_run(monkeypatch, message: str, caplog=None):
    import app.studio.service as service_module

    monkeypatch.setattr(service_module, "AGUIAdapter", _AdapterFactory(message))
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    service = StudioService(repository, Settings(studio_test_mode=True))
    run_id = uuid.uuid4()
    if caplog is not None:
        with caplog.at_level(logging.WARNING, logger="studio"):
            response = await service.stream_request(None, _payload(conversation["id"], run_id))
            async for _event in response.body_iterator:
                pass
    else:
        response = await service.stream_request(None, _payload(conversation["id"], run_id))
        async for _event in response.body_iterator:
            pass
    return repository, await repository.get_run(run_id)


class _AdapterFactory:
    """Wrap a fixed RUN_ERROR message in the AGUIAdapter interface."""

    def __init__(self, message: str):
        self._adapter = _RunErrorAdapter(message)

    def __call__(self, **kwargs):
        return self._adapter

    def __getattr__(self, name: str):
        return getattr(_RunErrorAdapter, name)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "code", "status"),
    [
        ("status_code: 401, model_name: x/y, body: unauthorized", "provider_auth_failed", 401),
        ("status_code: 402, model_name: x/y, body: insufficient credits", "provider_payment_required", 402),
        ("status_code: 404, model_name: x/y, body: not found", "provider_model_not_found", 404),
        ("status_code: 429, model_name: x/y, body: rate limited", "provider_rate_limited", 429),
        ("status_code: 503, model_name: x/y, body: unavailable", "provider_unavailable", 503),
    ],
)
async def test_upstream_statuses_keep_category_and_diagnostics(monkeypatch, message, code, status):
    repository, run = await _failed_run(monkeypatch, message)
    assert run is not None and run["status"] == "failed"
    assert run["error_code"] == code
    assert run["error_phase"] == "startup"
    assert run["error_status"] == status
    assert run["error_class"] in ("ModelHTTPError", "UpstreamProviderError")
    events = await repository.get_events(run["id"])
    errors = [event for event in events if event["event_type"] == "RUN_ERROR"]
    assert errors and errors[0]["safe_payload"]["diagnostics"]["error_code"] == code


@pytest.mark.asyncio
async def test_zero_tool_call_provider_error_is_not_generic(monkeypatch):
    repository, run = await _failed_run(
        monkeypatch, "status_code: 404, model_name: x/y, body: no endpoints found for x/y"
    )
    assert run is not None
    assert run["error_code"] == "provider_model_not_found"
    assert run["error_phase"] == "startup"


@pytest.mark.asyncio
async def test_unknown_exception_stays_generic_but_keeps_class_and_phase(monkeypatch):
    repository, run = await _failed_run(monkeypatch, "something completely unexpected")
    assert run is not None
    assert run["error_code"] == "agent_failed"
    assert run["error_phase"] == "startup"
    assert run["error_class"] == "UpstreamProviderError"
    assert run.get("error_status") is None


@pytest.mark.asyncio
async def test_timeout_and_malformed_output_classify(monkeypatch):
    import app.studio.service as service_module

    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    service = StudioService(repository, Settings(studio_test_mode=True))

    class _TimeoutAdapter:
        build_run_input = staticmethod(AGUIAdapter.build_run_input)

        def __init__(self, **_kwargs):
            pass

        async def run_stream(self, **kwargs):
            raise asyncio.TimeoutError("provider timed out")
            yield  # pragma: no cover

        def encode_stream(self, stream):
            return stream

    monkeypatch.setattr(service_module, "AGUIAdapter", _TimeoutAdapter)
    run_id = uuid.uuid4()
    response = await service.stream_request(None, _payload(conversation["id"], run_id))
    async for _event in response.body_iterator:
        pass
    run = await repository.get_run(run_id)
    assert run is not None and run["error_code"] == "provider_timeout"

    class _MalformedAdapter:
        build_run_input = staticmethod(AGUIAdapter.build_run_input)

        def __init__(self, **_kwargs):
            pass

        async def run_stream(self, **kwargs):
            raise ValueError("not json at all {{{")
            yield  # pragma: no cover

        def encode_stream(self, stream):
            return stream

    monkeypatch.setattr(service_module, "AGUIAdapter", _MalformedAdapter)
    run_id = uuid.uuid4()
    response = await service.stream_request(None, _payload(conversation["id"], run_id))
    async for _event in response.body_iterator:
        pass
    run = await repository.get_run(run_id)
    assert run is not None and run["error_code"] == "invalid_agent_output"


@pytest.mark.asyncio
async def test_tool_execution_phase_recorded_after_tool_start(monkeypatch):
    import app.studio.service as service_module
    from ag_ui.core import RunStartedEvent

    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    service = StudioService(repository, Settings(studio_test_mode=True))

    class _ToolThenErrorAdapter:
        build_run_input = staticmethod(AGUIAdapter.build_run_input)

        def __init__(self, **_kwargs):
            pass

        async def run_stream(self, **kwargs):
            yield RunStartedEvent(thread_id=kwargs["conversation_id"], run_id=kwargs["run_id"])
            yield SimpleNamespace(type="TOOL_CALL_START", tool_name="search_web", tool_call_id="c1")
            yield SimpleNamespace(type="RUN_ERROR", message="status_code: 500, model_name: x/y, body: boom")

        def encode_stream(self, stream):
            return stream

    monkeypatch.setattr(service_module, "AGUIAdapter", _ToolThenErrorAdapter)
    run_id = uuid.uuid4()
    response = await service.stream_request(None, _payload(conversation["id"], run_id))
    async for _event in response.body_iterator:
        pass
    run = await repository.get_run(run_id)
    assert run is not None and run["error_code"] == "provider_unavailable"
    assert run["error_phase"] == "tool_execution"


@pytest.mark.asyncio
async def test_user_cancel_records_cancelled_diagnostics(monkeypatch):
    import app.studio.service as service_module

    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    service = StudioService(repository, Settings(studio_test_mode=True))

    class _CancelAdapter:
        build_run_input = staticmethod(AGUIAdapter.build_run_input)

        def __init__(self, **_kwargs):
            pass

        async def run_stream(self, **kwargs):
            await repository.request_cancel(uuid.UUID(str(kwargs["run_id"])))
            raise asyncio.CancelledError()
            yield  # pragma: no cover

        def encode_stream(self, stream):
            return stream

    monkeypatch.setattr(service_module, "AGUIAdapter", _CancelAdapter)
    run_id = uuid.uuid4()
    response = await service.stream_request(None, _payload(conversation["id"], run_id))
    async for _event in response.body_iterator:
        pass
    run = await repository.get_run(run_id)
    assert run is not None and run["status"] == "cancelled"
    assert run["error_code"] == "run_cancelled"
    assert run["error_phase"] == "startup"


@pytest.mark.asyncio
async def test_no_secrets_prompts_or_bodies_persisted(monkeypatch, caplog):
    secret = "sk-or-fake-secret-123"
    repository, run = await _failed_run(
        monkeypatch,
        f"status_code: 401, model_name: x/y, body: {{\"key\": \"{secret}\"}}",
        caplog=caplog,
    )
    assert run is not None
    blob = json.dumps(run, default=str)
    assert secret not in blob
    assert "sk-or-" not in blob
    events = await repository.get_events(run["id"])
    assert secret not in json.dumps(events, default=str)
    for record in caplog.records:
        assert secret not in str(getattr(record, "msg", ""))
        observation = getattr(record, "studio_observation", None)
        if observation is not None:
            assert secret not in json.dumps(observation, default=str)
