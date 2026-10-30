"""emet-cli check: reports what is set up, and with --last whether the last turn was captured. Sends nothing."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from emet_cli._project import DELIVERY_FILE, ENV_FILE, KEY_FILE, has_env_value, python_files
from emet_sdk._configure import KEY_VARIABLE
from emet_sdk._delivery import GAP_KEYS, SCHEMA_VERSION, captured

SDK_DEPENDENCY = re.compile(r"""(?m)^[^#\n]*["']?emet[-_]sdk\b""")
CONFIGURE_CALL = re.compile(r"\bemet_sdk\.configure\(|\bfrom emet_sdk import[^\n]*\bconfigure\b")
TURN_CALL = re.compile(r"\bemet_sdk\.turn\(|\bfrom emet_sdk import[^\n]*\bturn\b")
DELIVERY_CALL = re.compile(r"\bwrite_delivery_file\(")
DEPENDENCY_FILES = ("pyproject.toml", "requirements.txt", "requirements.in", "setup.py", "setup.cfg")

INSTRUMENT = "Call Agent.instrument_all() at startup so Pydantic AI emits the spans Emet reads."
REPORT_IT = "Report it with this result at https://github.com/FUSEis/emet-py/issues."
RELOGIN = "Run `uvx emet-cli login` again with a current key."
RESTART = (
    f"Restart your app from this folder with {ENV_FILE} loaded (for example `uvicorn app:app --env-file .env`), "
    "send one question through the code that calls emet_sdk.turn(), then run this again."
)

# One line per reason the SDK can give: what happened, then the fix
SKIPPED = {
    "no_spans": ("turn() ran, but nothing inside it emitted spans.", INSTRUMENT),
    "no_genai_spans": ("turn() ran, but no AI framework call happened inside it.", INSTRUMENT),
    "tenant_ref_too_long": ("tenant_ref was over 255 characters.", "Pass the Team's External ID as tenant_ref."),
    "state_cap": (
        "The turn was over the SDK's limits: too many turns in progress, or too many spans in one turn.",
        REPORT_IT,
    ),
    "span_never_ended": ("A span started inside turn() never ended.", "End every span you start inside turn()."),
}
STOPPED = {
    "unreachable": (
        "Your app could not reach Emet.",
        "Check the network. In a sandbox, allow app.emet.so in its network settings.",
    ),
    "timeout": ("Emet did not answer in time.", "Send another question; if it keeps happening, check the network."),
    "rate_limited": ("Emet rate limited this Application.", "Wait a few minutes, then send another question."),
    "server_error": ("Emet answered with a server error.", "Send another question in a minute."),
    "span_too_large": ("One span was over Emet's 32 MB request limit.", REPORT_IT),
    "unexpected_response": ("Emet answered in a way the SDK did not expect.", REPORT_IT),
    "sdk_error": ("The SDK failed while sending the turn.", REPORT_IT),
}
REFUSED_KEY = ("authentication_failed", "not_authenticated")


def check(root: Path, last: bool, now: datetime | None = None) -> tuple[int, dict[str, Any]]:
    """Returns the exit code and the report: 0 when everything checked is fine."""
    report: dict[str, Any] = {"folder": str(root), "setup": _setup(root)}
    oks = [f["ok"] for f in report["setup"]["findings"]]
    if last:
        report["last"] = _last(root, now or datetime.now(timezone.utc))
        oks.append(report["last"]["captured"])
    report["ok"] = all(oks)
    return (0 if report["ok"] else 1), report


def render(report: dict[str, Any]) -> str:
    """Formats a report for a person reading the terminal."""
    lines = [f"Emet setup in {report['folder']}"]
    rows = [*report["setup"]["findings"]]
    if "last" in report:
        rows.append({"ok": report["last"]["captured"], "what": report["last"]["what"], "fix": report["last"]["fix"]})
    for row in rows:
        lines.append(f"  {'ok ' if row['ok'] else 'fix'}  {row['what']}")
        if row["fix"]:
            lines.append(f"       {row['fix']}")
    return "\n".join(lines)


def _setup(root: Path) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    key = _read_json(root / KEY_FILE)
    key_saved = has_env_value(root / ENV_FILE, KEY_VARIABLE)
    if key_saved and key is not None:
        state = "checked with Emet" if key.get("verified") else "saved, not checked with Emet"
        findings.append(_f(True, f"Key {key.get('prefix', '?')}... {state} on {key.get('checked_at', '?')}."))
    elif key_saved:
        findings.append(_f(True, f"{KEY_VARIABLE} is set in {ENV_FILE}."))
    else:
        findings.append(
            _f(False, f"No {KEY_VARIABLE} in {ENV_FILE}.", "Run `uvx emet-cli login` yourself, in this folder.")
        )

    depends = any(SDK_DEPENDENCY.search(_read(root / name)) for name in DEPENDENCY_FILES)
    findings.append(
        _f(True, "The app depends on emet-sdk.")
        if depends
        else _f(
            False,
            "The app does not list emet-sdk as a dependency.",
            "Add it: `uv add emet-sdk` or `pip install emet-sdk`.",
        )
    )

    sources = {path: _read(path) for path in python_files(root)}
    configure = sorted(_rel(root, p) for p, text in sources.items() if CONFIGURE_CALL.search(text))
    turns = sorted(_rel(root, p) for p, text in sources.items() if TURN_CALL.search(text))
    deliveries = sorted(_rel(root, p) for p, text in sources.items() if DELIVERY_CALL.search(text))
    findings.append(
        _f(True, f"emet_sdk.configure() is called in {', '.join(configure)}.")
        if configure
        else _f(False, "Nothing calls emet_sdk.configure().", "Call it once at startup, after your own tracing setup.")
    )
    findings.append(
        _f(True, f"emet_sdk.turn() is used in {', '.join(turns)}.")
        if turns
        else _f(False, "Nothing calls emet_sdk.turn().", "Wrap each agent run in `with emet_sdk.turn(...)`.")
    )
    findings.append(
        _f(True, "The app saves delivery results for `check --last`.")
        if deliveries
        else _f(
            False,
            "The app does not save delivery results, so `check --last` cannot see them.",
            f'Pass on_delivery=emet_sdk.write_delivery_file("{DELIVERY_FILE}") to emet_sdk.configure().',
        )
    )
    return {
        "key_saved": key_saved,
        "key_prefix": (key or {}).get("prefix", ""),
        "key_verified": bool((key or {}).get("verified")),
        "depends_on_sdk": depends,
        "configure_in": configure,
        "turn_in": turns,
        "saves_delivery_in": deliveries,
        "findings": findings,
    }


def _last(root: Path, now: datetime) -> dict[str, Any]:
    result = _read_json(root / DELIVERY_FILE)
    base: dict[str, Any] = {"found": result is not None, "captured": False, "age_seconds": None, "result": result}
    if result is None:
        return {
            **base,
            "what": "No turn has been reported yet.",
            "fix": RESTART,
        }
    if result.get("schema_version") != SCHEMA_VERSION:
        return {
            **base,
            "what": "The last result was written by a different emet-sdk.",
            "fix": "Use the same version of emet-cli and emet-sdk.",
        }
    age = _age(result.get("written_at"), now)
    trace = result.get("trace_id") or "the turn"
    what, fix = _explain(result, trace)
    when = f" ({_ago(age)})" if age is not None else ""
    return {**base, "captured": captured(result), "age_seconds": age, "what": what + when, "fix": fix}


def _explain(result: dict[str, Any], trace: str) -> tuple[str, str]:
    status, reason, code = result.get("status"), result.get("reason", ""), result.get("code", "")
    if status == "skipped":
        what, fix = SKIPPED.get(reason, (f"The SDK skipped the turn ({reason}).", REPORT_IT))
        return f"Not sent: {what}", fix
    if status == "stopped":
        if reason == "http_4xx":
            if code in REFUSED_KEY:
                return "Emet refused the key.", RELOGIN
            if code == "permission_denied":
                return (
                    "Emet refused the turn for this key.",
                    "If the key is pinned to an environment, set deployment.environment.name to match it. " + RELOGIN,
                )
            return f"Emet refused the turn as invalid ({code or 'no code'}).", REPORT_IT
        what, fix = STOPPED.get(reason, (f"Sending stopped ({reason}).", REPORT_IT))
        done, left = result.get("batches_sent", 0), result.get("batches_not_sent", 0)
        sent = f" {done} of {done + left} batches arrived."
        return f"Not captured: {what}{sent}", fix
    problems: list[tuple[str, str]] = []
    if result.get("unattributed"):
        problems.append(
            (
                "tenant_ref matched no Team in Emet",
                "Compare the tenant_ref you pass to turn() with the Team's External ID in Emet.",
            )
        )
    gaps = cast(dict[str, list[str]], result.get("gaps") or {})
    for key in GAP_KEYS:
        if gaps.get(key):
            problems.append(
                (
                    f"{key.replace('_', ' ')}: {', '.join(gaps[key])}",
                    "Keep content capture on in your instrumentation, so tool calls carry their arguments and results.",
                )
            )
    if result.get("late"):
        problems.append(("part of the turn arrived after Emet closed the trace", "Send another question."))
    if not problems and result.get("late_after_retry"):
        return (
            f"Probably captured: {trace} was retried and Emet had already closed it.",
            "Look for the trace id under AI usage, Recent runs.",
        )
    if not problems:
        return f"Captured: Emet took all of {trace} ({result.get('accepted', 0)} spans).", ""
    return (
        f"Emet received {trace}, but {'; '.join(p for p, _ in problems)}.",
        " ".join(dict.fromkeys(f for _, f in problems)),
    )


def _f(ok: bool, what: str, fix: str = "") -> dict[str, Any]:
    return {"ok": ok, "what": what, "fix": fix}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _age(written_at: Any, now: datetime) -> int | None:
    try:
        when = datetime.fromisoformat(str(written_at))
    except ValueError:
        return None
    return int((now - when).total_seconds()) if when.tzinfo else None


def _ago(seconds: int) -> str:
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes ago"
    if seconds < 172800:
        return f"{round(seconds / 3600)} hours ago"
    return f"{round(seconds / 86400)} days ago"
