"""Opt-in OpenRouter smoke test for the M0 provider boundary.

The normal application and test suite use the local fake model.  This module
only constructs an OpenAI-compatible PydanticAI model when the caller both
provides a key and explicitly enables the smoke script.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from ..config import Settings


class OpenRouterSmokeResult(BaseModel):
    status: Literal["ok", "skipped", "error"]
    model: str = ""
    output_length: int = 0
    reason: str = ""


async def run_openrouter_smoke(settings: Settings) -> OpenRouterSmokeResult:
    """Make one bounded provider request, returning no provider payloads."""

    api_key = settings.openrouter_api_key.strip()
    if not api_key:
        return OpenRouterSmokeResult(
            status="skipped",
            reason="OPENROUTER_API_KEY is not configured",
        )

    model_name = settings.openrouter_model.strip() or "openai/gpt-4o-mini"
    try:
        model = OpenAIChatModel(
            model_name,
            provider=OpenAIProvider(
                base_url=settings.openrouter_base_url.strip()
                or "https://openrouter.ai/api/v1",
                api_key=api_key,
            ),
        )
        agent = Agent(
            model,
            output_type=str,
            instructions=(
                "Reply with exactly the word READY. This is a connectivity smoke "
                "test; do not include secrets or channel data."
            ),
        )
        result = await agent.run(
            "Reply with exactly READY.",
            usage_limits=UsageLimits(request_limit=1, output_tokens_limit=16),
        )
        return OpenRouterSmokeResult(
            status="ok",
            model=model_name,
            output_length=len(result.output or ""),
        )
    except Exception as exc:  # noqa: BLE001 - convert provider failures to safe status
        return OpenRouterSmokeResult(
            status="error",
            model=model_name,
            reason=exc.__class__.__name__,
        )
