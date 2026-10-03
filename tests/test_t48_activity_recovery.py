"""T48 truthful activity, stream recovery and editorial feedback contracts.

Activity renders actual run/tool events as inline Thinking with elapsed time
and Stop — never a staged checklist, fabricated percent, synthetic timers or
a forced tool order. Terminal events settle pending activity; cancel is
server-confirmed (no local cancelled state); reconnect reconciles stored
state with duplicate suppression; failures keep drafts/history with an
actionable retry or setup path. T40–T42 partial-engine health, claim checks
and safe diagnostics stay intact.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "studio-frontend/src/main.tsx").read_text(encoding="utf-8")


def test_activity_has_no_staged_workflow_vocabulary():
    lowered = SOURCE.lower()
    assert "checklist" not in lowered
    assert "percent" not in lowered
    assert "% complete" not in lowered
    assert "step 1" not in lowered
    assert "stage 1 of" not in lowered
    assert "workflow" not in lowered
    assert "synthetic" not in lowered


def test_activity_renders_thinking_elapsed_and_stop_from_real_events():
    assert "function AgentActivity" in SOURCE or "export function AgentActivity" in SOURCE
    assert "studio-agent-elapsed" in SOURCE
    assert "Stop run" in SOURCE
    assert "describeAgentActivity(run, events)" in SOURCE
    assert 'role="status" aria-live="polite"' in SOURCE


def test_cancel_is_server_confirmed_without_local_cancelled_state():
    assert "/studio/api/runs/${encodeURIComponent(runId)}/cancel" in SOURCE
    assert '"Stopping…"' in SOURCE
    # A failed cancel returns to Stop; settled status arrives from the server.
    assert ".catch(() => setStopping(false))" in SOURCE
    assert 'status: "cancelled"' not in SOURCE
    assert "status: 'cancelled'" not in SOURCE


def test_reconnect_reconciles_with_duplicate_suppression():
    assert "mergeRunEvents(current, payload.events)" in SOURCE
    assert "setLinkDown(true)" in SOURCE
    assert "Connection lost while following this run" in SOURCE
    assert "Reconnect" in SOURCE
    assert "reconnectToken" in SOURCE


def test_failures_keep_work_with_retry_or_setup_paths():
    assert "Try again with the same request" in SOURCE
    assert "Your saved draft and chat history are kept" in SOURCE
    assert "focusComposer()" in SOURCE
    assert "isSetupBlockerCode(recoveredRun.error_code)" in SOURCE
    assert "Check settings" in SOURCE


def test_partial_search_and_model_failure_stay_distinct():
    assert "describeSearchOutcome" in SOURCE
    assert "describeRunFailure" in SOURCE
    assert "studio-run-error" in SOURCE
    assert "searchNotice" in SOURCE or "describeSearchOutcome(shown.activity)" in SOURCE


def test_final_answer_and_artifact_stay_separate_from_activity():
    assert "<ThreadPrimitive.Messages" in SOURCE
    assert "<DraftPanel" in SOURCE
    # Run completion only reconciles; it never forces draft creation and the
    # tool labels below merely describe observed agent events.
    assert 'method: "POST"' not in SOURCE.split("handleRunFinished")[1].split("}, [refresh]);")[0]
    assert "/studio/api/profile/build" not in SOURCE
