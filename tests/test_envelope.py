from __future__ import annotations

import json
from typing import Any

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanContext, Status, StatusCode

from emet_sdk._envelope import build_envelope
from emet_sdk._ids import identifier
from emet_sdk._processor import CapturedTurn
from emet_sdk._redact import redact
from emet_sdk._turn import TurnInfo
from tests.fixtures import BASE_TIME, fixture_names, load_turn, read, write


@pytest.mark.parametrize("name", list(fixture_names()))
def test_fixture_builds_its_envelope(name: str, request: pytest.FixtureRequest) -> None:
    envelope = build_envelope(load_turn(read(name, "spans")))
    if request.config.getoption("--update-fixtures"):
        write(name, "envelope", envelope)
    assert envelope == read(name, "envelope")


def tool_turn() -> dict[str, Any]:
    envelope = build_envelope(load_turn(read("pydantic_ai_tool_turn", "spans")))
    assert envelope is not None
    return envelope


def by_name(envelope: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["name"]: s for s in envelope["spans"]}


def test_the_header_comes_from_turn_and_the_resource() -> None:
    envelope = tool_turn()
    assert envelope["tenant_ref"] == "team_editorial"
    # Not the UUID Pydantic AI put on the spans
    assert envelope["conversation_id"] == "thread-1"
    assert envelope["enduser_ref"] == "ada@acme.example"
    assert (envelope["ingestion_mode"], envelope["semconv_version"], envelope["end"]) == ("sdk", "emet-genai-1", True)
    assert envelope["resource"]["service.name"] == "support-agent"
    assert envelope["resource"]["telemetry.sdk.name"] == "emet-sdk"


def test_the_agent_span_is_the_turn_and_holds_no_payload() -> None:
    root = tool_turn()["spans"][0]
    assert (root["kind"], root["operation"], root["parent_span_id"]) == ("turn", "invoke_agent", "")
    assert "payload" not in root
    assert all(s["parent_span_id"] == root["span_id"] for s in tool_turn()["spans"][1:])


def test_tool_payloads() -> None:
    spans = by_name(tool_turn())
    assert spans["execute_tool get_spend"]["payload"] == {
        "arguments": {"channel": "a"},
        "result": {"spend": 48210.55, "currency": "USD", "channel": "a"},
    }
    # A non-object result travels as its JSON string, and so does a None result
    assert spans["execute_tool get_daily"]["payload"]["result"] == "[6800,7100,6950]"
    assert spans["execute_tool mark_reviewed"]["payload"]["result"] == "null"


def test_secrets_are_redacted_and_counted() -> None:
    envelope = tool_turn()
    text = json.dumps(envelope)
    assert "hunter2" not in text
    assert "sk-ant-" not in text
    assert envelope["spans"][0]["attributes"]["emet.redactions"] >= 2


def test_the_system_prompt_travels_as_a_hash() -> None:
    envelope = tool_turn()
    model = next(s for s in envelope["spans"] if s["kind"] == "model")
    assert "You answer spend questions." not in json.dumps(envelope)
    assert "Be brief." not in json.dumps(envelope)
    [part] = model["payload"]["system_instructions"]
    assert (part["type"], part["name"], len(part["content"])) == ("prompt_hash", "sha256", 64)
    assert all(m["role"] != "system" for m in model["payload"]["input_messages"])


def test_media_is_described_by_its_decoded_bytes() -> None:
    from emet_sdk._redact import media_descriptor

    assert media_descriptor("image/png", "aGVs\nbG8=") == media_descriptor("image/png", "aGVsbG8=")
    assert "5 bytes" in media_descriptor("image/png", "aGVsbG8=")


def test_an_image_becomes_a_descriptor() -> None:
    model = next(s for s in tool_turn()["spans"] if s["kind"] == "model")
    blob = next(p for m in model["payload"]["input_messages"] for p in m["parts"] if p["type"] == "blob")
    assert blob["content"].startswith("[image/png not sent: 48 bytes, sha256 ")


def test_tool_responses_inside_messages_are_named_response() -> None:
    last_model = [s for s in tool_turn()["spans"] if s["kind"] == "model"][-1]
    responses = [
        p for m in last_model["payload"]["input_messages"] for p in m["parts"] if p["type"] == "tool_call_response"
    ]
    assert all("result" not in p for p in responses)
    # Pydantic AI leaves the outcome out when a tool returned None
    assert ["response" in p for p in responses] == [True, True, False, True]


def test_the_last_model_call_is_the_final_output() -> None:
    models = [s for s in tool_turn()["spans"] if s["kind"] == "model"]
    assert [m.get("is_final_output", False) for m in models] == [False, True]


def test_framework_attributes_are_dropped() -> None:
    text = json.dumps(tool_turn())
    for dropped in ("pydantic_ai.all_messages", "logfire.", "model_request_parameters", "gen_ai.conversation.id"):
        assert dropped not in text


def test_a_failed_tool_keeps_its_arguments_and_loses_its_stack_trace() -> None:
    envelope = build_envelope(load_turn(read("pydantic_ai_failed_tool", "spans")))
    assert envelope is not None
    tool = by_name(envelope)["execute_tool broken"]
    assert tool["status"] == "error"
    assert "arguments" in tool["payload"] and "result" not in tool["payload"]
    assert tool["error_type"] == "ConnectionError"
    assert "hunter2" not in tool["error_message"]
    assert "Traceback" not in json.dumps(envelope)
    # A Team External ID with spaces is sent as is; a conversation id Emet would refuse is hashed
    assert envelope["tenant_ref"] == "Team Editorial"
    assert envelope["conversation_id"] == identifier("thread #42/ü", 255)
    assert envelope["enduser_ref"] == ""


def span(
    span_id: int,
    parent: int | None,
    name: str,
    attributes: dict[str, Any] | None = None,
    start: int = 0,
    end: int = 1,
    status: StatusCode = StatusCode.UNSET,
) -> ReadableSpan:
    return ReadableSpan(
        name=name,
        context=SpanContext(1, span_id, is_remote=False),
        parent=SpanContext(1, parent, is_remote=False) if parent else None,
        resource=Resource({}),
        attributes=attributes or {},
        status=Status(status),
        start_time=BASE_TIME + start * 1_000_000,
        end_time=BASE_TIME + end * 1_000_000,
    )


def turn_of(*spans: ReadableSpan, tenant_ref: str = "t", user: str | None = None) -> CapturedTurn:
    return CapturedTurn(TurnInfo(1, tenant_ref, "c", user), spans)


def op(name: str, **extra: Any) -> dict[str, Any]:
    return {"gen_ai.operation.name": name, **extra}


def test_spans_without_a_genai_marker_are_dropped_and_skipped_over() -> None:
    envelope = build_envelope(
        turn_of(
            span(1, None, "POST /chat", start=0, end=10),
            span(2, 1, "invoke_agent", op("invoke_agent"), start=1, end=9),
            span(3, 2, "db query", start=2, end=3),
            span(4, 3, "chat", op("chat"), start=2, end=3),
        )
    )
    assert envelope is not None
    assert [(s["name"], s["parent_span_id"]) for s in envelope["spans"]] == [
        ("invoke_agent", ""),
        ("chat", "0000000000000002"),
    ]


def test_several_agent_runs_fold_into_one_turn() -> None:
    envelope = build_envelope(
        turn_of(
            span(1, None, "invoke_agent first", op("invoke_agent"), start=0, end=5),
            span(2, None, "invoke_agent second", op("invoke_agent"), start=6, end=9),
        )
    )
    assert envelope is not None
    first, second = envelope["spans"]
    assert (first["kind"], second["kind"], second["parent_span_id"]) == ("turn", "agent", first["span_id"])
    # The turn spans the earliest start and the latest end
    assert first["started_at"] < second["started_at"]
    assert first["ended_at"] == second["ended_at"]


def test_a_turn_with_no_genai_span_builds_nothing() -> None:
    assert build_envelope(turn_of(span(1, None, "POST /chat"))) is None


def test_an_overlong_tenant_ref_skips_the_turn() -> None:
    assert build_envelope(turn_of(span(1, None, "chat", op("chat")), tenant_ref="x" * 256)) is None


def test_a_bare_model_call_keeps_its_payload_under_a_turn_of_its_own() -> None:
    output = json.dumps([{"role": "assistant", "parts": [{"type": "text", "content": "Hi"}]}])
    envelope = build_envelope(turn_of(span(1, None, "chat", op("chat", **{"gen_ai.output.messages": output}))))
    assert envelope is not None
    turn, model = envelope["spans"]
    assert (turn["span_id"], turn["kind"], turn["name"]) == ("turn.0000000000000001", "turn", "emet.turn")
    assert (model["parent_span_id"], model["kind"], model["is_final_output"]) == (turn["span_id"], "model", True)
    assert model["payload"]["output_messages"]


def test_the_turn_lasts_until_its_last_call_ends() -> None:
    envelope = build_envelope(
        turn_of(
            span(1, None, "agent", op("invoke_agent"), start=0, end=5),
            span(2, 1, "chat", op("chat"), start=1, end=8),
        )
    )
    assert envelope is not None
    assert envelope["spans"][0]["ended_at"] == envelope["spans"][1]["ended_at"]


def test_a_failed_model_call_is_not_the_final_output() -> None:
    output = json.dumps([{"role": "assistant", "parts": [{"type": "text", "content": "Partial"}]}])
    envelope = build_envelope(
        turn_of(
            span(1, None, "agent", op("invoke_agent")),
            span(2, 1, "chat", op("chat", **{"gen_ai.output.messages": output}), status=StatusCode.ERROR),
        )
    )
    assert envelope is not None
    assert "is_final_output" not in envelope["spans"][1]


def test_the_final_output_uses_exact_end_times() -> None:
    output = json.dumps([{"role": "assistant", "parts": [{"type": "text", "content": "Hi"}]}])
    first = span(2, 1, "chat a", op("chat", **{"gen_ai.output.messages": output}), start=1, end=2)
    second = span(3, 1, "chat b", op("chat", **{"gen_ai.output.messages": output}), start=1, end=2)
    # Same microsecond, a few nanoseconds apart
    second._end_time = (second.end_time or 0) + 5
    envelope = build_envelope(turn_of(span(1, None, "agent", op("invoke_agent")), first, second))
    assert envelope is not None
    assert [s.get("is_final_output", False) for s in envelope["spans"][1:]] == [False, True]


def test_a_status_description_never_carries_a_traceback() -> None:
    failed = ReadableSpan(
        name="execute_tool t",
        context=SpanContext(1, 2, is_remote=False),
        parent=SpanContext(1, 1, is_remote=False),
        attributes=op("execute_tool"),
        status=Status(StatusCode.ERROR, "boom\nTraceback (most recent call last):\n  File x"),
        start_time=BASE_TIME,
        end_time=BASE_TIME,
    )
    envelope = build_envelope(turn_of(span(1, None, "agent", op("invoke_agent")), failed))
    assert envelope is not None
    assert envelope["spans"][1]["error_message"] == "boom"


def test_plain_text_system_instructions_are_hashed() -> None:
    envelope = build_envelope(turn_of(span(1, None, "chat", op("chat", **{"gen_ai.system_instructions": "Be brief."}))))
    assert envelope is not None
    assert envelope["spans"][1]["payload"]["system_instructions"][0]["type"] == "prompt_hash"
    assert "Be brief." not in json.dumps(envelope)


def test_a_turn_over_the_span_cap_is_skipped() -> None:
    captured = turn_of(span(1, None, "chat", op("chat")))
    assert build_envelope(CapturedTurn(captured.info, captured.spans, dropped_spans=1)) is None


def test_metadata_strings_are_redacted() -> None:
    secret = "sk-ant-" + "cd34" * 6
    attributes = op("chat", **{"gen_ai.response.finish_reasons": (secret,), "session.id": secret})
    envelope = build_envelope(turn_of(span(1, None, "chat", attributes)))
    assert envelope is not None
    assert secret not in json.dumps(envelope)


def test_values_under_sensitive_keys_are_redacted() -> None:
    result = json.dumps({"password": "correcthorsebatterystaple", "max_tokens": 5, "user": "ada"})
    envelope = build_envelope(
        turn_of(
            span(1, None, "agent", op("invoke_agent")),
            span(2, 1, "execute_tool t", op("execute_tool", **{"gen_ai.tool.call.result": result})),
        )
    )
    assert envelope is not None
    assert envelope["spans"][1]["payload"]["result"] == {
        "password": "[REDACTED:secret_field]",
        "max_tokens": 5,
        "user": "ada",
    }


def test_an_overlong_session_ref_is_hashed() -> None:
    attributes = op("chat", **{"session.id": "s" * 300})
    envelope = build_envelope(turn_of(span(1, None, "chat", attributes)))
    assert envelope is not None
    assert len(envelope["session_ref"]) == 255 and envelope["session_ref"] != "s" * 255


def test_an_overlong_user_is_left_out() -> None:
    envelope = build_envelope(turn_of(span(1, None, "chat", op("chat")), user="x" * 256))
    assert envelope is not None and envelope["enduser_ref"] == ""


def test_model_columns_and_a_non_stop_finish() -> None:
    attributes = op(
        "chat",
        **{
            "gen_ai.request.model": "gpt-x",
            "gen_ai.provider.name": "openai",
            "gen_ai.usage.input_tokens": 12,
            "gen_ai.usage.output_tokens": -1,
            "gen_ai.usage.cache_read.input_tokens": 4,
            "gen_ai.response.finish_reasons": ("length",),
            "gen_ai.response.id": "resp_1",
            "gen_ai.request.temperature": 0.2,
            "gen_ai.output.messages": json.dumps([{"role": "assistant", "parts": [{"type": "text", "content": "Hi"}]}]),
        },
    )
    envelope = build_envelope(turn_of(span(1, None, "agent", op("invoke_agent")), span(2, 1, "chat", attributes)))
    assert envelope is not None
    model = envelope["spans"][1]
    assert (model["model_name"], model["provider"], model["input_tokens"], model["cache_read_tokens"]) == (
        "gpt-x",
        "openai",
        12,
        4,
    )
    assert "output_tokens" not in model
    assert (model["finish_reason"], model["response_id"]) == ("length", "resp_1")
    assert model["attributes"] == {"gen_ai.request.model": "gpt-x", "gen_ai.request.temperature": 0.2}
    assert "is_final_output" not in model


def test_retrieval_and_memory_payloads() -> None:
    envelope = build_envelope(
        turn_of(
            span(1, None, "agent", op("invoke_agent")),
            span(
                2,
                1,
                "retrieval",
                op(
                    "retrieval",
                    **{"gen_ai.retrieval.query.text": "spend", "gen_ai.retrieval.documents": '[{"id": "d1"}]'},
                ),
            ),
            span(3, 1, "memory", op("search_memory", **{"gen_ai.memory.query.text": "q"})),
        )
    )
    assert envelope is not None
    _, retrieval, memory = envelope["spans"]
    assert retrieval["payload"] == {"query": "spend", "documents": [{"id": "d1"}]}
    assert (memory["kind"], memory["operation"], memory["payload"]) == ("memory", "search_memory", {"query": "q"})


def test_attributes_are_kept_under_the_size_limit() -> None:
    attributes = op("chat", **{f"gen_ai.request.k{i}": "x" * 1024 for i in range(40)})
    envelope = build_envelope(turn_of(span(1, None, "chat", attributes)))
    assert envelope is not None
    kept = envelope["spans"][1]["attributes"]
    assert len(json.dumps(kept).encode()) <= 16384
    assert all(len(v) <= 1024 for v in kept.values())


def test_identifiers() -> None:
    assert identifier("thread-1", 255) == "thread-1"
    hashed = identifier("thread #1", 255)
    assert hashed.startswith("thread__1:") and len(hashed) == len("thread__1:") + 16
    # Two raw ids that sanitize alike stay apart, and one raw id always maps the same
    assert identifier("a b", 64) != identifier("a/b", 64)
    assert identifier("a b", 64) == identifier("a b", 64)
    assert len(identifier("x" * 300, 64)) == 64
    assert identifier("...", 64).startswith("...:")
    # A raw id already shaped like a hashed one is hashed as well, so it can never equal another id's hash
    assert identifier("a_b:" + "0" * 16, 64) != "a_b:" + "0" * 16


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("token sk-ant-" + "ab12" * 6, "anthropic_key"),
        ("AKIA" + "ABCDEFGHIJKLMNOP", "aws_access_key"),
        ("Authorization: Bearer abcdefghijklmnop1234", "bearer_token"),
        ("postgres://user:pw@host/db", "url_password"),
        ("API_KEY=abc123def456ghi789", "secret_assignment"),
        ("ab12CD34.0123456789abcdefABCDEF0123456789", "emet_key"),
    ],
)
def test_redaction_vectors(text: str, kind: str) -> None:
    redacted, count = redact(text)
    assert f"[REDACTED:{kind}]" in redacted
    assert count == 1
