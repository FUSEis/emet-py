"""Spans-in, envelope-out fixtures: serialized spans with deterministic ids and clocks, and the envelope they build."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.trace import SpanContext, Status, StatusCode

from emet_sdk._processor import CapturedTurn
from emet_sdk._turn import TurnInfo

FIXTURES = Path(__file__).parent / "fixtures"
BASE_TIME = 1_800_000_000_000_000_000
FIXED_TRACE_ID = 0x0AF7651916CD43DD8448EB211C80319C


def dump_spans(spans: Sequence[ReadableSpan], turn: dict[str, Any]) -> dict[str, Any]:
    """Serializes spans with ids numbered and clocks replaced by their order, so a recording is stable."""
    ordered = sorted(spans, key=lambda s: (s.start_time or 0, s.context.span_id if s.context else 0))
    span_ids = {s.context.span_id: index + 1 for index, s in enumerate(ordered) if s.context}
    times = sorted({t for s in ordered for t in (s.start_time, s.end_time) if t is not None})
    clock = {t: BASE_TIME + index * 1_000_000 for index, t in enumerate(times)}
    out: list[dict[str, Any]] = []
    for span in ordered:
        assert span.context is not None
        parent = span.parent.span_id if span.parent else None
        out.append(
            {
                "name": span.name,
                "span_id": span_ids[span.context.span_id],
                "parent_span_id": span_ids.get(parent) if parent else None,
                "start_time": clock[span.start_time or 0],
                "end_time": clock[span.end_time or 0],
                "status": {"code": span.status.status_code.name, "description": span.status.description},
                "attributes": dict(span.attributes or {}),
                "events": [{"name": e.name, "attributes": dict(e.attributes or {})} for e in span.events],
                "resource": dict(span.resource.attributes),
            }
        )
    return {"turn": turn, "spans": out}


def load_turn(data: dict[str, Any]) -> CapturedTurn:
    """Rebuilds the captured turn a fixture describes."""
    turn = data["turn"]
    info = TurnInfo(1, turn["tenant_ref"], turn["conversation_id"], turn.get("user"))
    spans: list[ReadableSpan] = []
    for span in data["spans"]:
        parent = span.get("parent_span_id")
        spans.append(
            ReadableSpan(
                name=span["name"],
                context=SpanContext(FIXED_TRACE_ID, span["span_id"], is_remote=False),
                parent=SpanContext(FIXED_TRACE_ID, parent, is_remote=False) if parent else None,
                resource=Resource.create(span.get("resource", {})),
                attributes=_attributes(span.get("attributes", {})),
                events=[Event(e["name"], _attributes(e.get("attributes", {}))) for e in span.get("events", [])],
                status=Status(StatusCode[span["status"]["code"]], span["status"].get("description")),
                start_time=span["start_time"],
                end_time=span["end_time"],
            )
        )
    return CapturedTurn(info, tuple(spans))


def _attributes(raw: dict[str, Any]) -> dict[str, Any]:
    # JSON has no tuples; OpenTelemetry stores sequences as tuples
    return {key: tuple(value) if isinstance(value, list) else value for key, value in raw.items()}


def fixture_names() -> Iterable[str]:
    return sorted(path.name.removesuffix(".spans.json") for path in FIXTURES.glob("*.spans.json"))


def read(name: str, suffix: str) -> Any:
    return json.loads((FIXTURES / f"{name}.{suffix}.json").read_text(encoding="utf-8"))


def write(name: str, suffix: str, data: Any) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    (FIXTURES / f"{name}.{suffix}.json").write_text(text, encoding="utf-8")
