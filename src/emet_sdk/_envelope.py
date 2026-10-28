# Decoded JSON is untyped by nature
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""Builds Emet's v1 envelope from a captured turn's GenAI spans, following otel-mapping.md in the emet repo."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import StatusCode

from emet_sdk._ids import CONVERSATION_ID_MAX, SPAN_ID_MAX, TRACE_ID_MAX, identifier
from emet_sdk._processor import CapturedTurn
from emet_sdk._redact import Scrubber, media_descriptor
from emet_sdk._version import VERSION

logger = logging.getLogger("emet_sdk")

SEMCONV_VERSION = "emet-genai-1"
SDK_NAME = "emet-sdk"
OPERATION = "gen_ai.operation.name"

OPERATION_KINDS = {
    "invoke_workflow": "workflow",
    "create_agent": "agent",
    "invoke_agent": "agent",
    "plan": "plan",
    "chat": "model",
    "generate_content": "model",
    "text_completion": "model",
    "fetch_response": "model",
    "execute_tool": "tool",
    "retrieval": "retrieval",
    "search_memory": "memory",
    "create_memory": "memory",
    "update_memory": "memory",
    "upsert_memory": "memory",
    "delete_memory": "memory",
    "create_memory_store": "memory",
    "delete_memory_store": "memory",
    "embeddings": "embedding",
}

# Attributes copied onto the span's attributes; everything else, such as a framework's own message dumps, is dropped
ATTRIBUTE_PREFIXES = ("gen_ai.agent.", "gen_ai.request.", "gen_ai.output.type", "gen_ai.data_source.", "server.")
ATTRIBUTE_TEXT_MAX = 1024
ATTRIBUTES_MAX_BYTES = 16384
MEDIA_PART_TYPES = ("blob", "binary", "file", "image")
MAX_TOKENS = 2_147_483_647
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def build_envelope(captured: CapturedTurn, secrets: tuple[str, ...] = ()) -> dict[str, Any] | None:
    """Returns the envelope for one turn, or None when it has nothing Emet can take."""
    info = captured.info
    if len(info.tenant_ref) > 255:
        logger.warning("Emet skipped a turn: tenant_ref is over 255 characters")
        return None
    spans = sorted(captured.spans, key=lambda s: (s.start_time or 0, _span_id(s)))
    candidates = [s for s in spans if isinstance(_attrs(s).get(OPERATION), str)]
    if not candidates:
        return None
    candidate_ids = {_span_id(s) for s in candidates}
    parents = {_span_id(s): _parent_id(s) for s in spans}

    def candidate_parent(span: ReadableSpan) -> str:
        parent = parents.get(_span_id(span), "")
        while parent and parent not in candidate_ids:
            parent = parents.get(parent, "")
        return parent

    tops = [s for s in candidates if not candidate_parent(s)]
    root = tops[0]
    root_id = _span_id(root)
    scrub = Scrubber(secrets)
    out: list[dict[str, Any]] = []
    for span in candidates:
        entry = _span(span, scrub)
        if span is root:
            entry["kind"] = "turn"
            entry.pop("payload", None)
            entry["started_at"] = _time(min(s.start_time or 0 for s in tops))
            entry["ended_at"] = _time(max(s.end_time or s.start_time or 0 for s in tops))
        else:
            entry["parent_span_id"] = candidate_parent(span) or root_id
        out.append(entry)
    _mark_final_output(out)
    if scrub.redactions:
        out[0].setdefault("attributes", {})["emet.redactions"] = scrub.redactions

    trace_id = identifier(f"{root.context.trace_id:032x}-{root_id}" if root.context else root_id, TRACE_ID_MAX)
    envelope: dict[str, Any] = {
        "tenant_ref": info.tenant_ref,
        "conversation_id": identifier(info.conversation_id, CONVERSATION_ID_MAX),
        "trace_id": trace_id,
        "enduser_ref": info.user if info.user and len(info.user) <= 255 else "",
        "ingestion_mode": "sdk",
        "semconv_version": SEMCONV_VERSION,
        "resource": _resource(root),
        "spans": out,
        "end": True,
    }
    session = _attrs(root).get("session.id")
    if isinstance(session, str) and session:
        envelope["session_ref"] = session[:255]
    return envelope


def _attrs(span: ReadableSpan) -> Mapping[str, Any]:
    return span.attributes or {}


def _span_id(span: ReadableSpan) -> str:
    return identifier(f"{span.context.span_id:016x}", SPAN_ID_MAX) if span.context else ""


def _parent_id(span: ReadableSpan) -> str:
    return f"{span.parent.span_id:016x}" if span.parent else ""


def _time(nanoseconds: int) -> str:
    moment = _EPOCH + timedelta(microseconds=nanoseconds // 1000)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _text(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _tokens(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return min(value, MAX_TOKENS)


def _span(span: ReadableSpan, scrub: Scrubber) -> dict[str, Any]:
    attrs = _attrs(span)
    operation = str(attrs[OPERATION])
    kind = OPERATION_KINDS.get(operation, "unknown")
    entry: dict[str, Any] = {
        "span_id": _span_id(span),
        "parent_span_id": "",
        "name": scrub.text(span.name)[:255],
        "kind": kind,
        "operation": operation[:32],
        "status": "error" if span.status.status_code is StatusCode.ERROR else "ok",
        "started_at": _time(span.start_time or 0),
        "ended_at": _time(span.end_time or span.start_time or 0),
    }
    columns = {
        "model_name": _text(attrs.get("gen_ai.response.model") or attrs.get("gen_ai.request.model"), 255),
        "provider": _text(attrs.get("gen_ai.provider.name") or attrs.get("gen_ai.system"), 100),
        "tool_name": _text(attrs.get("gen_ai.tool.name"), 255),
        "tool_call_id": _text(attrs.get("gen_ai.tool.call.id"), 255),
        "response_id": _text(attrs.get("gen_ai.response.id"), 255),
    }
    entry.update({key: scrub.text(value) for key, value in columns.items() if value})
    if kind == "model":
        for key, attribute in (
            ("input_tokens", "gen_ai.usage.input_tokens"),
            ("output_tokens", "gen_ai.usage.output_tokens"),
            ("cache_read_tokens", "gen_ai.usage.cache_read.input_tokens"),
            ("cache_write_tokens", "gen_ai.usage.cache_creation.input_tokens"),
        ):
            count = _tokens(attrs.get(attribute))
            if count is not None:
                entry[key] = count
        reasons = attrs.get("gen_ai.response.finish_reasons")
        if isinstance(reasons, Sequence) and reasons and isinstance(reasons[0], str):
            entry["finish_reason"] = reasons[0][:32]
    entry.update(_error(span, scrub))
    attributes = _attributes(attrs, scrub)
    if attributes:
        entry["attributes"] = attributes
    payload = _payload(kind, attrs, scrub)
    if payload:
        entry["payload"] = payload
    return entry


def _error(span: ReadableSpan, scrub: Scrubber) -> dict[str, str]:
    if span.status.status_code is not StatusCode.ERROR:
        return {}
    event = next((e for e in span.events if e.name == "exception"), None)
    event_attrs: Mapping[str, Any] = (event.attributes or {}) if event else {}
    error_type = _text(event_attrs.get("exception.type") or _attrs(span).get("error.type"), 64)
    # The stack trace is never sent: it names files and local variables
    message = _text(event_attrs.get("exception.message") or span.status.description, 10000)
    result = {}
    if error_type:
        result["error_type"] = scrub.text(error_type)
    if message:
        result["error_message"] = scrub.text(message)
    return result


def _attributes(attrs: Mapping[str, Any], scrub: Scrubber) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for key in sorted(attrs):
        if not key.startswith(ATTRIBUTE_PREFIXES):
            continue
        value = attrs[key]
        if isinstance(value, str):
            kept[key] = value[:ATTRIBUTE_TEXT_MAX]
        elif isinstance(value, bool | int | float):
            kept[key] = value
        elif isinstance(value, Sequence):
            kept[key] = [v[:ATTRIBUTE_TEXT_MAX] if isinstance(v, str) else v for v in value]
    kept = scrub(kept)
    while kept and len(json.dumps(kept, ensure_ascii=False).encode("utf-8")) > ATTRIBUTES_MAX_BYTES:
        kept.pop(max(kept, key=lambda k: len(json.dumps(kept[k], ensure_ascii=False))))
    return kept


def _decode(value: Any) -> Any:
    """Returns a JSON attribute as the value it encodes, or the raw value when it is not JSON."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    if isinstance(value, Sequence):
        return list(value)
    return value


def _object_or_string(value: Any) -> Any:
    """An object stays an object; anything else travels as its JSON string, as Emet requires."""
    if isinstance(value, str):
        decoded = _decode(value)
        return decoded if isinstance(decoded, dict) else value
    if isinstance(value, dict) or value is None:
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _payload(kind: str, attrs: Mapping[str, Any], scrub: Scrubber) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if kind == "tool":
        for key, attribute in (("arguments", "gen_ai.tool.call.arguments"), ("result", "gen_ai.tool.call.result")):
            if attribute in attrs:
                payload[key] = _object_or_string(attrs[attribute])
    elif kind == "model":
        system_texts: list[str] = []
        for key, attribute in (
            ("input_messages", "gen_ai.input.messages"),
            ("output_messages", "gen_ai.output.messages"),
        ):
            messages = _messages(_decode(attrs.get(attribute)))
            if messages is None:
                continue
            if key == "input_messages":
                system_texts += [t for m in messages if m.get("role") == "system" for t in _part_texts(m.get("parts"))]
                messages = [m for m in messages if m.get("role") != "system"]
            payload[key] = messages
        system_texts += _part_texts(_decode(attrs.get("gen_ai.system_instructions")))
        if system_texts:
            digest = hashlib.sha256("\n\n".join(system_texts).encode("utf-8")).hexdigest()
            payload["system_instructions"] = [{"type": "prompt_hash", "name": "sha256", "content": digest}]
        definitions = _decode(attrs.get("gen_ai.tool.definitions"))
        if isinstance(definitions, list):
            payload["tool_definitions"] = [d for d in definitions if isinstance(d, dict)]
    elif kind in ("retrieval", "memory"):
        prefix = "gen_ai.retrieval." if kind == "retrieval" else "gen_ai.memory."
        query = attrs.get(f"{prefix}query.text")
        if isinstance(query, str):
            payload["query"] = query
        items = _decode(attrs.get(f"{prefix}documents" if kind == "retrieval" else f"{prefix}records"))
        if isinstance(items, list):
            payload["documents" if kind == "retrieval" else "records"] = items
    return scrub(payload)


def _part_texts(parts: Any) -> list[str]:
    if not isinstance(parts, list):
        return []
    return [
        p["content"]
        for p in parts
        if isinstance(p, dict) and p.get("type") in ("text", None) and isinstance(p.get("content"), str)
    ]


def _messages(raw: Any) -> list[dict[str, Any]] | None:
    """Returns the messages in the shape Emet validates, or None when the attribute holds no list."""
    if not isinstance(raw, list):
        return None
    messages: list[dict[str, Any]] = []
    for message in raw:
        if not isinstance(message, dict):
            continue
        out: dict[str, Any] = {k: v for k, v in message.items() if k not in ("role", "parts", "finish_reason")}
        if isinstance(message.get("role"), str):
            out["role"] = message["role"]
        if isinstance(message.get("finish_reason"), str):
            out["finish_reason"] = message["finish_reason"]
        parts = message.get("parts")
        if isinstance(parts, list):
            out["parts"] = [_part(p) for p in parts if isinstance(p, dict)]
        messages.append(out)
    return messages


def _part(raw: dict[str, Any]) -> dict[str, Any]:
    part = dict(raw)
    kind = part.get("type")
    # Pydantic AI names the outcome result; the convention, and Emet, read response
    if kind == "tool_call_response" and "response" not in part and "result" in part:
        part["response"] = part.pop("result")
    for key in ("arguments", "response"):
        if key in part:
            part[key] = _object_or_string(part[key])
    if kind in MEDIA_PART_TYPES and isinstance(part.get("content"), str):
        media = part.get("mime_type") or part.get("media_type") or kind
        part["content"] = media_descriptor(str(media), part["content"])
    for key in ("type", "content", "name", "role"):
        if key in part and part[key] is not None and not isinstance(part[key], str):
            part[key] = json.dumps(part[key], ensure_ascii=False, default=str)
    if "id" in part and part["id"] is not None:
        part["id"] = str(part["id"])
    return part


def _mark_final_output(spans: list[dict[str, Any]]) -> None:
    """Marks the turn's last model call as the final output when it answered in text and stopped normally."""
    models = [s for s in spans if s["kind"] == "model"]
    if not models:
        return
    last = max(models, key=lambda s: s["ended_at"])
    if last.get("finish_reason", "stop") != "stop":
        return
    parts = [p for m in last.get("payload", {}).get("output_messages", []) for p in m.get("parts", [])]
    if any(p.get("type") == "tool_call" for p in parts):
        return
    if any(p.get("type") in ("text", None) and p.get("content") for p in parts):
        last["is_final_output"] = True


def _resource(span: ReadableSpan) -> dict[str, Any]:
    attrs = span.resource.attributes
    resource: dict[str, Any] = {
        key: str(attrs[key])[:255]
        for key in ("service.name", "service.version", "deployment.environment.name")
        if isinstance(attrs.get(key), str) and attrs[key]
    }
    resource["telemetry.sdk.name"] = SDK_NAME
    resource["telemetry.sdk.version"] = VERSION
    return resource
