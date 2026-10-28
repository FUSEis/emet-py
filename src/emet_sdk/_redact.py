"""Secret redaction and cleanup applied to every string Emet receives. Patterns are ported from emet-plugin."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable
from typing import Any

# (kind, pattern, group kept in front of the redaction)
EMET_KEY = re.compile(r"\b[A-Za-z0-9]{8}\.[A-Za-z0-9]{32}\b")
SECRET_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
        0,
    ),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), 0),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{32,}"), 0),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), 0),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b"), 0),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"), 0),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), 0),
    ("slack_webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+"), 0),
    ("stripe_key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"), 0),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), 0),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"), 0),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), 0),
    ("emet_key", EMET_KEY, 0),
    ("bearer_token", re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=\-]{16,}"), 1),
    ("url_password", re.compile(r"([a-z][a-z0-9+.\-]*://[^/\s:@]+:)[^/\s@]+(?=@)"), 1),
    # NAME=value where the name says secret. The value needs a digit and must not be a call or subscript
    (
        "secret_assignment",
        re.compile(
            r"(?i)(\b[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY)[A-Z0-9_]*\s*[:=]\s*[\"']?)"
            r"(?=[A-Za-z0-9_\-./+=]*\d)[A-Za-z0-9_\-./+=]{12,}(?![A-Za-z0-9_\-./+=(\[])"
        ),
        1,
    ),
]

# A long run of base64 characters mixing cases and digits is file content, not text
BASE64_RUN = re.compile(r"^(?=[^a-z]*[a-z])(?=[^A-Z]*[A-Z])(?=[^0-9]*[0-9])[A-Za-z0-9+/\r\n]{4096,}={0,2}\s*$")
LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def redact(text: str, secrets: tuple[str, ...] = ()) -> tuple[str, int]:
    """Returns the text with secrets replaced by [REDACTED:kind], and how many were replaced."""
    count = 0
    for secret in secrets:
        if secret and secret in text:
            count += text.count(secret)
            text = text.replace(secret, "[REDACTED:emet_key]")
    for kind, pattern, keep in SECRET_PATTERNS:

        def replace(match: re.Match[str], kind: str = kind, keep: int = keep) -> str:
            return f"{match.group(keep) if keep else ''}[REDACTED:{kind}]"

        text, replaced = pattern.subn(replace, text)
        count += replaced
    return text, count


def clean_text(text: str) -> str:
    """Drops NUL characters and replaces lone surrogates, both of which Emet rejects."""
    return LONE_SURROGATE.sub("�", text.replace("\x00", ""))


def media_descriptor(media_type: str, data: str) -> str:
    """Describes inline file content by type, size and digest instead of sending it."""
    size = len(data) * 3 // 4 - data.count("=")
    digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
    return f"[{media_type or 'binary'} not sent: {size} bytes, sha256 {digest}]"


def walk(value: Any, transform: Callable[[str], str]) -> Any:
    """Applies transform to every string, keys included, and turns non-finite numbers into null."""
    if isinstance(value, str):
        return transform(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {
            transform(key) if isinstance(key, str) else key: walk(item, transform)
            for key, item in value.items()  # pyright: ignore[reportUnknownVariableType]
        }
    if isinstance(value, list):
        return [walk(item, transform) for item in value]  # pyright: ignore[reportUnknownVariableType]
    return value


class Scrubber:
    """Redacts and cleans every string, counting redactions."""

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        self.secrets = secrets
        self.redactions = 0

    def text(self, text: str) -> str:
        if BASE64_RUN.match(text):
            return media_descriptor("base64", text.strip())
        text, count = redact(clean_text(text), self.secrets)
        self.redactions += count
        return text

    def __call__(self, value: Any) -> Any:
        return walk(value, self.text)
