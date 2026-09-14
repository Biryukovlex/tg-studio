from __future__ import annotations

import asyncio
import json
import logging
import uuid
from types import SimpleNamespace

import pytest
from ag_ui.core import RunStartedEvent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.ui.ag_ui import AGUIAdapter

from app.config import Settings
from app.studio.observability import safe_error
from app.studio.repository import MemoryStudioRepository
from app.studio.service import StudioService


def _payload(conversation_id: uuid.UUID, run_id: uuid.UUID, content: str = "run fixture") -> bytes:
    return json.dumps(
        {
            "threadId": str(conversation_id),
            "runId": str(run_id),
            "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": content}],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        }
    ).encode()


def test_provider_http_errors_are_classified_without_leaking_body():
    for status, expected in ((401, "provider_auth_failed"), (402, "provider_payment_required"), (404, "provider_model_not_found")):
        code, message, retryable = safe_error(
            ModelHTTPError(status, "x/y", {"secret": "https://private.example/body"})
        )
        assert code == expected
        assert "private.example" not in message
        assert "body" not in message
        assert retryable is False
    code, message, retryable = safe_error(ModelHTTPError(400, "x/y", {"secret": "body"}))
    assert (code, retryable) == ("provider_bad_request", True)
    assert "body" not in message


class _ProviderErrorAdapter:
    build_run_input = staticmethod(AGUIAdapter.build_run_input)

    def __init__(self, **_kwargs):
        pass

    async def run_stream(self, **kwargs):
        yield RunStartedEvent(thread_id=kwargs["conversation_id"], run_id=kwargs["run_id"])
        yield SimpleNamespace(
            type="RUN_ERROR",
            message="status_code: 404, model_name: x/y, body: https://private.example/body",
        )

    def encode_stream(self, stream):
        return stream


@pytest.mark.asyncio
async def test_provider_run_error_keeps_safe_model_message_and_logs_metadata(monkeypatch, caplog):
    monkeypatch.setattr("app.studio.service.AGUIAdapter", _ProviderErrorAdapter)
    repository = MemoryStudioRepository()
    conversation = await repository.create_conversation(channel_id=1)
    service = StudioService(repository, Settings(studio_test_mode=True))
    run_id = uuid.uuid4()
    with caplog.at_level(logging.WARNING, logger="studio"):
        response = await service.stream_request(None, _payload(conversation["id"], run_id))
        async for _event in response.body_iterator:
            pass

    run = await repository.get_run(run_id)
    assert run and run["status"] == "failed"
    assert run["error_code"] == "provider_model_not_found"
    assert "not available on OpenRouter" in run["error_message"]
    assert "private.example" not in run["error_message"]
    assert "body" not in run["error_message"]
    observations = [record.studio_observation for record in caplog.records if hasattr(record, "studio_observation")]
    failed = [item for item in observations if item.get("status") == "failed"]
    assert len(failed) == 1
    assert failed[0]["error_code"] == "provider_model_not_found"
    assert failed[0]["exception_class"] == "_UpstreamRunError"
    assert failed[0]["status_code"] == 404
    assert "private.example" not in str(failed[0])
    assert "body" not in str(failed[0])


class _ClaimLostRepository(MemoryStudioRepository):
    async def claim_run(self, run_id, *, worker_id, lease_seconds=120):
        row = self.runs[run_id]
        row.update({"status": "interrupted", "stage": "interrupted", "error_code": "run_interrupted", "error_message": "The previous worker stopped before completion."})
        return None


@pytest.mark.asyncio
async def test_claim_lost_after_sweep_closes_stream_with_terminal_notice(monkeypatch):
    class _NeverStartedAdapter:
        build_run_input = staticmethod(AGUIAdapter.build_run_input)

        def __init__(self, **_kwargs):
            pass

        async def run_stream(self, **_kwargs):
            raise AssertionError("a swept run must not start the provider")
            yield  # pragma: no cover

        def encode_stream(self, stream):
            return stream

    monkeypatch.setattr("app.studio.service.AGUIAdapter", _NeverStartedAdapter)
    repository = _ClaimLostRepository()
    conversation = await repository.create_conversation(channel_id=1)
    service = StudioService(repository, Settings(studio_test_mode=True))
    run_id = uuid.uuid4()
    response = await service.stream_request(None, _payload(conversation["id"], run_id))
    events = []
    async with asyncio.timeout(2):
        async for event in response.body_iterator:
            events.append(event)
    assert events[-1].type == "RUN_ERROR"
    assert events[-1].code == "run_interrupted"
    messages = await repository.list_messages(conversation["id"])
    assert messages[-1]["role"] == "assistant"
    assert "stopped before completion" in messages[-1]["content"]
