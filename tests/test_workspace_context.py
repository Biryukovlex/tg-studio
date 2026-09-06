import uuid

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from app.web.dependencies import WorkspaceContext, assert_workspace_id, require_workspace_context


def _request(app: FastAPI, session: dict) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "server": ("test", 80),
        "client": ("test", 1),
        "scheme": "http",
        "app": app,
        "session": session,
    }
    return Request(scope)


def test_workspace_context_requires_auth_and_fails_closed_for_other_workspace():
    app = FastAPI()
    workspace_id = uuid.uuid4()
    app.state.workspace_context = WorkspaceContext(
        user_id=uuid.uuid4(), workspace_id=workspace_id, workspace_slug="community", role="owner"
    )
    with pytest.raises(Exception) as unauthenticated:
        require_workspace_context(_request(app, {}))
    assert getattr(unauthenticated.value, "status_code", None) == 303

    context = require_workspace_context(_request(app, {"auth": True}))
    assert context.workspace_id == workspace_id
    assert_workspace_id(context, workspace_id)
    with pytest.raises(Exception) as cross_tenant:
        assert_workspace_id(context, uuid.uuid4())
    assert getattr(cross_tenant.value, "status_code", None) == 404
