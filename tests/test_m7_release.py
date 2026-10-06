"""M7 release-runbook and entry-point contracts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_release_scripts_are_runnable_from_repository_root():
    for name in ("restore_history.py",):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / name), "--help"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "usage:" in result.stdout.lower()


def test_release_runbook_preserves_postgres_backup_and_restore_contract():
    runbook = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")

    assert "--format=custom" in runbook
    assert "pg_restore --clean --if-exists --no-owner" in runbook
    assert "docker compose down -v" in runbook
    assert "never use" in runbook.lower()
