"""mark_result_truncated(): tells Emet that a tool returned only part of what it read."""

from __future__ import annotations

from opentelemetry import trace

from emet_sdk._envelope import TRUNCATED_ATTRIBUTE


def mark_result_truncated() -> None:
    """Marks the current tool call's result as cut short, so Emet treats it as incomplete evidence.

    Call it inside a tool that clips what it returns, such as a result capped at a length. Emet then never reads a
    figure missing from the result as one the model invented. The mark is a boolean on the current span, so call it
    from the tool's own code, not inside a nested span. Outside a recording span it does nothing.
    """
    trace.get_current_span().set_attribute(TRUNCATED_ATTRIBUTE, True)
