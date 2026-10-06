"""Capture safe exception metadata before AG-UI turns errors into text."""
from pydantic_ai.ui.ag_ui import AGUIAdapter, AGUIEventStream

from .diagnostics import build_run_diagnostics
from .observability import safe_error


class StudioAGUIAdapter(AGUIAdapter):
    failure_diagnostics: dict | None = None
    safe_failure: tuple[str, str, bool] | None = None

    def build_event_stream(self):
        adapter = self

        class DiagnosticStream(AGUIEventStream):
            async def on_error(self, error):
                adapter.failure_diagnostics = build_run_diagnostics(error)
                adapter.safe_failure = safe_error(error)
                # Raw exception text stays inside the service boundary. Its
                # consumer replaces this event with a safe persisted notice.
                async for event in super().on_error(error):
                    yield event

        return DiagnosticStream(self.run_input, accept=self.accept, ag_ui_version=self.ag_ui_version)
