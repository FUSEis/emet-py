"""The delivery result: what happened to one turn, for on_delivery and `emet-cli check --last`."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("emet_sdk")

SCHEMA_VERSION = 1
GAP_KEYS = ("missing_tool_arguments", "missing_tool_results", "conflicting_final_outputs")

# Never holds the key, tenant_ref, conversation id, user, or any request or response body
DeliveryResult = dict[str, Any]


def new_result(status: str, reason: str = "", trace_id: str = "") -> DeliveryResult:
    """Every result has the same keys, whatever its status."""
    return {
        "schema_version": SCHEMA_VERSION,
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "reason": reason,
        "code": "",
        "trace_id": trace_id,
        "trace": "",
        "conversation": "",
        "accepted": 0,
        "batches_sent": 0,
        "batches_not_sent": 0,
        "unattributed": False,
        "late": False,
        "late_after_retry": False,
        "gaps": {key: [] for key in GAP_KEYS},
    }


def captured(result: DeliveryResult) -> bool:
    """Whether Emet took the whole turn, attributed to a Team, with no gaps."""
    return (
        result["status"] == "sent"
        and not (result["late"] or result["late_after_retry"] or result["unattributed"])
        and not any(result["gaps"].values())
    )


def write_delivery_file(path: str | os.PathLike[str]) -> Callable[[DeliveryResult], None]:
    """Returns an on_delivery callback that writes each result to path, for `emet-cli check --last`.

    The path is resolved now, against the working directory. Its folder must exist; write errors are ignored.
    """
    target = Path(path).resolve()

    def write(result: DeliveryResult) -> None:
        try:
            fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".emet-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    json.dump(result, file, indent=2)
                os.replace(temp, target)
            except BaseException:
                Path(temp).unlink(missing_ok=True)
                raise
        except OSError as error:
            logger.debug("Emet could not write the delivery file: %s", error.strerror)

    return write
