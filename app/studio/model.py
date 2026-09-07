"""PydanticAI model construction for the Studio.

OpenRouter is configured directly through PydanticAI's OpenAI-compatible
support. The deterministic TestModel is available only for explicit local
tests; it is never selected silently in a production configuration.
"""

from __future__ import annotations

from typing import Any

from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.providers.openai import OpenAIProvider

from .. import limits


class StudioConfigurationError(RuntimeError):
    """A safe, actionable configuration problem."""

    code = "studio_not_configured"
    public_message = "Studio is not configured for an agent run."


def build_model(settings, *, test_model: bool | None = None) -> Model:
    """Build the configured model without exposing secrets to callers."""

    use_test_model = settings.studio_test_mode if test_model is None else test_model
    if use_test_model:
        return TestModel(
            call_tools=["get_channel_context"],
            custom_output_text=(
                "I reviewed the selected channel context and can help plan the next post."
            ),
        )

    api_key = settings.openrouter_api_key.strip()
    if not api_key:
        raise StudioConfigurationError("OPENROUTER_API_KEY is required for Studio agent runs")
    model_name = settings.openrouter_model.strip() or "openai/gpt-4o-mini"
    return OpenAIChatModel(
        model_name,
        provider=OpenAIProvider(
            base_url=limits.OPENROUTER_BASE_URL,
            api_key=api_key,
        ),
    )


def model_name(settings) -> str:
    """Return a user-safe requested model label."""

    return settings.openrouter_model.strip() or "openai/gpt-4o-mini"

