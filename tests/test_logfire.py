from __future__ import annotations

import json
import subprocess
import sys

import pytest

SCRIPT = """
import json
import logfire
from opentelemetry import trace

import emet_sdk
from emet_sdk import _transport

def post(url, headers, body, timeout):
    answer = {"trace": "t", "conversation": "c", "accepted": len(json.loads(body)["spans"]), "gaps": {}}
    return _transport.Reply(202, {}, json.dumps(answer).encode())

_transport.urllib_post = post
logfire.configure(send_to_logfire=False, console=False)
results = []
emet_sdk.configure(on_delivery=results.append)
with emet_sdk.turn("team", "thread"), trace.get_tracer("t").start_as_current_span(
    "chat", attributes={"gen_ai.operation.name": "chat"}
):
    pass
assert emet_sdk.flush(5)
print(json.dumps(results[0]))
"""


def test_configure_works_after_logfire() -> None:
    pytest.importorskip("logfire")
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", SCRIPT],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        env={"EMET_APPLICATION_KEY": "emet_test_key_not_real", "PATH": ""},
    )
    result = json.loads(done.stdout.splitlines()[-1])
    assert (result["status"], result["accepted"]) == ("sent", 2)
