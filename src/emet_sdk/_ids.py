"""Identifiers Emet accepts: letters, digits and _ . : - only, within a length limit."""

from __future__ import annotations

import hashlib
import re

SPAN_ID_MAX = 64
TRACE_ID_MAX = 128
CONVERSATION_ID_MAX = 255

_VALID = re.compile(r"\A(?!\.+\Z)[A-Za-z0-9_.:-]+\Z")
_DISALLOWED = re.compile(r"[^A-Za-z0-9_.:-]")
_HASH_LENGTH = 16
# The shape of a hashed id; a raw id already in that shape is hashed too, so it can never equal another's
_HASHED = re.compile(r":[0-9a-f]{16}\Z")


def identifier(raw: str, limit: int) -> str:
    """Returns the id unchanged when Emet accepts it, otherwise a readable prefix and a hash of the raw id.

    Two different raw ids never map to the same value, and one raw id always maps to the same value.
    """
    if len(raw) <= limit and _VALID.match(raw) and not _HASHED.search(raw):
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_HASH_LENGTH]
    prefix = _DISALLOWED.sub("_", raw)[: limit - _HASH_LENGTH - 1]
    return f"{prefix}:{digest}"


def bounded(raw: str, limit: int) -> str:
    """Returns a free-text ref within the limit, hashing the rest of an overlong one so two never collide."""
    if len(raw) <= limit:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:_HASH_LENGTH]
    return f"{raw[: limit - _HASH_LENGTH - 1]}:{digest}"
