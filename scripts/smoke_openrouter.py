"""Run one explicitly opt-in OpenRouter connectivity smoke test."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import load_settings
from app.studio.provider import run_openrouter_smoke


def main() -> int:
    opt_in = os.environ.get("STUDIO_OPENROUTER_SMOKE", "").strip().lower()
    if opt_in not in {"1", "true", "yes"}:
        print(json.dumps({"status": "skipped", "reason": "explicit opt-in required"}))
        return 0
    result = asyncio.run(run_openrouter_smoke(load_settings()))
    print(json.dumps(result.model_dump(), sort_keys=True))
    return 0 if result.status in {"ok", "skipped"} else 1


if __name__ == "__main__":
    sys.exit(main())
