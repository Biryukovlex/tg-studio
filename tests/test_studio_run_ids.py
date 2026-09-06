"""Opaque AG-UI run identifiers map safely onto PostgreSQL UUID keys."""

from __future__ import annotations

import uuid

import pytest

from app.studio.run_ids import resolve_run_id


def test_uuid_run_id_is_preserved():
    workspace_id = uuid.uuid4()
    run_id = uuid.uuid4()

    assert resolve_run_id(str(run_id), workspace_id=workspace_id) == run_id


def test_opaque_run_id_is_stable_and_workspace_scoped():
    first_workspace = uuid.uuid4()
    second_workspace = uuid.uuid4()

    first = resolve_run_id("Lad6pwW", workspace_id=first_workspace)

    assert first == resolve_run_id("Lad6pwW", workspace_id=first_workspace)
    assert first != resolve_run_id("Lad6pwW", workspace_id=second_workspace)
    assert first.version == 5


@pytest.mark.parametrize("value", ["", "../run", "run id", "x" * 129])
def test_unsafe_run_ids_are_rejected(value: str):
    with pytest.raises(ValueError):
        resolve_run_id(value, workspace_id=uuid.uuid4())
