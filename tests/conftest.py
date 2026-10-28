from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Tracer

from emet_sdk import _turn
from emet_sdk._processor import CapturedTurn, EmetSpanProcessor


@dataclass
class Harness:
    """A local TracerProvider with the Emet processor next to another exporter, as in an app that already exports."""

    provider: TracerProvider
    tracer: Tracer
    processor: EmetSpanProcessor
    other_exporter: InMemorySpanExporter
    turns: list[CapturedTurn] = field(default_factory=lambda: [])


@pytest.fixture
def harness() -> Iterator[Harness]:
    turns: list[CapturedTurn] = []
    processor = EmetSpanProcessor(turns.append)
    other = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(processor)
    provider.add_span_processor(SimpleSpanProcessor(other))
    _turn._listeners.append(processor)
    try:
        yield Harness(provider, provider.get_tracer("test"), processor, other, turns)
    finally:
        _turn._listeners.remove(processor)
        provider.shutdown()
