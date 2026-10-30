from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from emet_cli._check import check, render
from emet_cli._main import main
from emet_sdk._delivery import new_result

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def app(tmp_path: Path) -> Path:
    """A Pydantic AI app wired for Emet, with a saved key."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\ndependencies = ["emet-sdk>=0.1"]\n')
    (tmp_path / ".env").write_text("EMET_APPLICATION_KEY=AbCd1234.secret\n")
    (tmp_path / ".emet").mkdir()
    key = {"prefix": "AbCd1234", "verified": True, "checked_at": "2026-10-30T11:00:00+00:00"}
    (tmp_path / ".emet" / "key.json").write_text(json.dumps(key))
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text(
        "import emet_sdk\n"
        'emet_sdk.configure(on_delivery=emet_sdk.write_delivery_file(".emet/last-delivery.json"))\n'
        "with emet_sdk.turn(tenant_ref=t, conversation_id=c, user=u):\n    pass\n"
    )
    return tmp_path


def deliver(app: Path, **fields: Any) -> None:
    result = {**new_result(fields.pop("status", "sent")), "trace_id": "abc-root", **fields}
    result["written_at"] = (NOW - timedelta(minutes=3)).isoformat()
    (app / ".emet" / "last-delivery.json").write_text(json.dumps(result))


def test_a_wired_app_passes_and_names_where(app: Path) -> None:
    code, report = check(app, last=False)
    assert code == 0
    setup = report["setup"]
    assert (setup["key_prefix"], setup["configure_in"], setup["turn_in"]) == (
        "AbCd1234",
        ["app/main.py"],
        ["app/main.py"],
    )
    assert "AbCd1234.secret" not in json.dumps(report)


def test_each_missing_piece_names_its_fix(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("pydantic-ai\n")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "lib.py").write_text("emet_sdk.configure()\n")
    code, report = check(tmp_path, last=False)
    assert code == 1
    fixes = " ".join(f["fix"] for f in report["setup"]["findings"])
    for fix in ("uvx emet-cli login", "uv add emet-sdk", "at startup", "emet_sdk.turn(", "write_delivery_file"):
        assert fix in fixes
    # Virtual environments are not the app's code
    assert report["setup"]["configure_in"] == []


def test_no_result_yet_says_how_to_produce_one(app: Path) -> None:
    code, report = check(app, last=True, now=NOW)
    assert code == 1
    assert report["last"]["what"] == "No turn has been reported yet."
    assert "Restart your app" in report["last"]["fix"]


def test_a_captured_turn(app: Path) -> None:
    deliver(app, accepted=7, batches_sent=1)
    code, report = check(app, last=True, now=NOW)
    assert code == 0
    assert report["last"]["what"] == "Captured: Emet took all of abc-root (7 spans). (3 minutes ago)"
    assert report["last"]["age_seconds"] == 180


@pytest.mark.parametrize(
    ("fields", "what", "fix"),
    [
        ({"unattributed": True}, "tenant_ref matched no Team", "External ID"),
        (
            {
                "gaps": {
                    "missing_tool_arguments": [],
                    "missing_tool_results": ["get_spend"],
                    "conflicting_final_outputs": [],
                }
            },
            "missing tool results: get_spend",
            "content capture",
        ),
        ({"late": True}, "after Emet closed the trace", "Send another question"),
        ({"late_after_retry": True}, "Probably captured", "Recent runs"),
        (
            {"status": "stopped", "reason": "unreachable", "batches_sent": 1, "batches_not_sent": 2},
            "1 of 3 batches",
            "app.emet.so",
        ),
        (
            {"status": "stopped", "reason": "http_4xx", "code": "authentication_failed"},
            "refused the key",
            "emet-cli login",
        ),
        ({"status": "stopped", "reason": "http_4xx", "code": "permission_denied"}, "for this key", "environment"),
        ({"status": "stopped", "reason": "http_4xx", "code": "invalid"}, "invalid (invalid)", "issues"),
        ({"status": "stopped", "reason": "sdk_error"}, "SDK failed", "issues"),
        ({"status": "stopped", "reason": "something_new"}, "something_new", "issues"),
        ({"status": "skipped", "reason": "no_spans"}, "nothing inside it emitted spans", "instrument_all"),
        ({"status": "skipped", "reason": "tenant_ref_too_long"}, "255 characters", "External ID"),
        ({"status": "skipped", "reason": "span_never_ended"}, "never ended", "End every span"),
    ],
)
def test_every_outcome_says_what_happened_and_the_fix(app: Path, fields: dict[str, Any], what: str, fix: str) -> None:
    deliver(app, **fields)
    code, report = check(app, last=True, now=NOW)
    # Only a whole, attributed, gap-free turn counts as captured
    assert code == 1
    assert what in report["last"]["what"]
    assert fix in report["last"]["fix"]


def test_a_result_from_another_sdk_version_is_not_read(app: Path) -> None:
    deliver(app, schema_version=2)
    _, report = check(app, last=True, now=NOW)
    assert "different emet-sdk" in report["last"]["what"]


def test_render_marks_each_line(app: Path) -> None:
    deliver(app, unattributed=True)
    _, report = check(app, last=True, now=NOW)
    text = render(report)
    assert "  ok   Key AbCd1234... checked with Emet" in text
    assert "  fix  Emet received abc-root, but tenant_ref matched no Team in Emet" in text


def test_the_command_line_prints_json_for_agents(
    app: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(app)
    deliver(app, accepted=2)
    assert main(["check", "--last", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["last"]["captured"] is True
    assert main(["check"]) == 0
    assert "emet_sdk.configure() is called in app/main.py." in capsys.readouterr().out


def test_the_command_line_reports_a_bad_endpoint(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["login", "--endpoint", "https://example.com"]) == 2
    assert "localhost" in capsys.readouterr().err
