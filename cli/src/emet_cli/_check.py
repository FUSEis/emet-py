"""emet-cli check: reports what is set up, and with --last whether the last turn was captured. Sends nothing."""

from __future__ import annotations

import ast
import json
import re
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from emet_cli._project import DELIVERY_FILE, ENV_FILE, KEY_FILE, env_value, has_env_value, python_files
from emet_sdk._configure import ENDPOINT_VARIABLE, KEY_VARIABLE
from emet_sdk._delivery import GAP_KEYS, SCHEMA_VERSION, captured
from emet_sdk._transport import DEFAULT_ENDPOINT, check_endpoint, resolve_endpoint

SDK_DEPENDENCY = re.compile(r"""(?m)^[^#\n]*["']?emet[-_]sdk\b""")
DELIVERY_CALL = re.compile(r"\bwrite_delivery_file\(")
# The first emet-sdk release that reads EMET_ENDPOINT
ENDPOINT_SDK = (0, 1, 2)
SDK_PIN = re.compile(r"""(?mi)^[^#\n]*?(?:^|["'\s])emet[-_]sdk\s*==\s*([0-9][0-9.]*)""")
# Only for files Python cannot parse here
CONFIGURE_TEXT = re.compile(r"\bemet_sdk\.configure\(|\bfrom emet_sdk import[^\n]*\bconfigure\b")
TURN_TEXT = re.compile(r"\bemet_sdk\.turn\(|\bfrom emet_sdk import[^\n]*\bturn\b")
SDK_LOCKED = re.compile(r'(?m)^name = "emet-sdk"\nversion = "([0-9][0-9.]*)"')
DEPENDENCY_FILES = ("pyproject.toml", "requirements.txt", "requirements.in", "setup.py", "setup.cfg")

INSTRUMENT = "Call Agent.instrument_all() at startup so Pydantic AI emits the spans Emet reads."
# Only the developer types the key, so every login fix is for them to run
ASK = "In your own terminal, run"
REPORT_IT = "Report it with this result at https://github.com/FUSEis/emet-py/issues."
RESTART = (
    f"Restart your app from this folder with {ENV_FILE} loaded (for example `uvicorn app:app --env-file .env`), "
    "send one question through the code that calls emet_sdk.turn(), then run this again."
)

# One line per reason the SDK can give: what happened, then the fix
SKIPPED = {
    "no_spans": (
        "turn() ran, but nothing inside it emitted spans.",
        f"{INSTRUMENT} If the agent streams, open turn() inside the generator that runs the stream.",
    ),
    "missing_turn_value": (
        "tenant_ref or conversation_id was empty, or not a string, integer or UUID.",
        "Pass the Team's External ID and your thread id, such as thread.id, from your server's own auth and storage.",
    ),
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

    scans = {path: _scan(_read(path)) for path in python_files(root)}
    app_scans = {path: scan for path, scan in scans.items() if not _is_test(root, path)}
    found = {scan.endpoint for scan in app_scans.values() if scan.configures}
    in_code = None if found <= {None} else (found.pop() if len(found) == 1 else UNREADABLE)
    sends_to, endpoint_findings = _endpoint_findings(root, key if key_saved else None, in_code)
    findings.extend(endpoint_findings)

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

    configure = sorted(_rel(root, p) for p, scan in app_scans.items() if scan.configures)
    turns = sorted(_rel(root, p) for p, scan in app_scans.items() if scan.turns)
    deliveries = sorted(_rel(root, p) for p in scans if DELIVERY_CALL.search(_read(p)))
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
        "sends_to": sends_to or "",
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
    key = _read_json(root / KEY_FILE) or {}
    what, fix = _explain(result, trace, _origin(key.get("endpoint")) or "")
    when = f" ({_ago(age)})" if age is not None else ""
    return {**base, "captured": captured(result), "age_seconds": age, "what": what + when, "fix": fix}


def _explain(result: dict[str, Any], trace: str, endpoint: str = "") -> tuple[str, str]:
    status, reason, code = result.get("status"), result.get("reason", ""), result.get("code", "")
    if status == "skipped":
        what, fix = SKIPPED.get(reason, (f"The SDK skipped the turn ({reason}).", REPORT_IT))
        return f"Not sent: {what}", fix
    if status == "stopped":
        if reason == "http_4xx":
            if code in REFUSED_KEY:
                return "Emet refused the key.", _relogin(endpoint)
            if code == "permission_denied":
                return (
                    "Emet refused the turn for this key.",
                    "If the key is pinned to an environment, set deployment.environment.name to match it. "
                    + _relogin(endpoint),
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


def _endpoint_findings(
    root: Path, key: dict[str, Any] | None, in_code: str | None
) -> tuple[str | None, list[dict[str, Any]]]:
    """Says where the app sends its turns, with the SDK's own rules, and whether that matches the key."""
    findings: list[dict[str, Any]] = []
    checked_with = _origin((key or {}).get("endpoint"))
    if checked_with is None:
        findings.append(
            _f(False, f"{KEY_FILE} names an Emet the SDK would refuse.", f"{ASK} `uvx emet-cli login` again.")
        )
    if in_code == UNREADABLE:
        findings.append(
            _f(
                True,
                "emet_sdk.configure() is given an endpoint check cannot read, so it cannot tell where turns go.",
                "Make sure it is the Emet the key came from, or remove endpoint= so the app follows the key.",
            )
        )
        return None, findings
    configured = env_value(root / ENV_FILE, ENDPOINT_VARIABLE)
    if in_code is None and "${" in configured:
        findings.append(
            _f(
                True,
                f"{ENDPOINT_VARIABLE} in {ENV_FILE} refers to another variable, so check cannot tell where turns go.",
                "Make sure it resolves to the Emet the key came from.",
            )
        )
        return None, findings
    if in_code is None and _origin(configured) != DEFAULT_ENDPOINT and configured.strip() and (old := _old_sdk(root)):
        # This SDK ignores EMET_ENDPOINT, valid or not, and sends to production; upgrading is the fix
        findings.append(old)
        return DEFAULT_ENDPOINT, findings
    try:
        sends_to, source = resolve_endpoint(in_code, configured)
    except ValueError:
        if in_code is not None:
            where, effect = "emet_sdk.configure(endpoint=...)", "the app will fail to start"
        else:
            where, effect = f"{ENDPOINT_VARIABLE} in {ENV_FILE}", "Emet will be off"
        what = f"{where} is not https://app.emet.so or a localhost address, so {effect}."
        findings.append(_f(False, what, "Remove it, or set it to your local Emet, such as http://localhost:8000."))
        return None, findings
    if checked_with and checked_with != sends_to:
        if in_code is not None:
            login = _login_command(checked_with)
            fix = (
                f"Remove endpoint= from emet_sdk.configure(), then {ASK.lower()} `{login}` so the app follows the key."
            )
        else:
            again, other = _login_command(checked_with), _login_command(sends_to)
            fix = f"{ASK} `{again}` again, or get a key from {sends_to} and run `{other}`."
        findings.append(_f(False, f"The key is from {checked_with}, but the app sends its turns to {sends_to}.", fix))
    else:
        origin = {
            ENDPOINT_VARIABLE: f" ({ENDPOINT_VARIABLE} in {ENV_FILE})",
            "the endpoint argument": " (set in emet_sdk.configure())",
        }
        findings.append(_f(True, f"The app sends its turns to {sends_to}{origin.get(source, '')}."))
    return sends_to, findings


def _old_sdk(root: Path) -> dict[str, Any] | None:
    """Flags an emet-sdk too old to read EMET_ENDPOINT; such an app sends to production."""
    found = _sdk_version(root)
    if found is None:
        return None
    version, where = found
    if version >= ENDPOINT_SDK:
        return None
    shown, needed = ".".join(map(str, version)), ".".join(map(str, ENDPOINT_SDK))
    fix = (
        f"Change the emet-sdk pin in {where} to {needed} or later, then install it."
        if where.startswith("requirements")
        else f"Upgrade to emet-sdk {needed} or later: run `uvx emet-cli init`."
    )
    return _f(False, f"The app uses emet-sdk {shown}, which ignores {ENDPOINT_VARIABLE} and sends to production.", fix)


@dataclass(frozen=True)
class _Scan:
    """What one source file does with emet_sdk, read with Python's own parser."""

    configures: bool = False
    turns: bool = False
    # The literal endpoint passed to emet_sdk.configure(), UNREADABLE when endpoint= is not a plain string
    endpoint: str | None = None


UNREADABLE = "<set in code>"


def _scan(text: str) -> _Scan:
    """Finds calls to emet_sdk.configure() and emet_sdk.turn(), under any import alias, ignoring comments."""
    if "emet_sdk" not in text:
        return _Scan()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text.removeprefix("\ufeff"))
    except (SyntaxError, ValueError):
        # Newer syntax than this Python reads: say what the text shows, and do not guess an endpoint
        aliased = bool(re.search(r"\bimport emet_sdk\b|\bfrom emet_sdk import\b", text))
        configures = bool(CONFIGURE_TEXT.search(text) or (aliased and re.search(r"\bconfigure\(", text)))
        turns = bool(TURN_TEXT.search(text) or (aliased and re.search(r"\bturn\(", text)))
        return _Scan(configures, turns, UNREADABLE if configures and "endpoint" in text else None)
    modules = {"emet_sdk"}
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.asname or a.name for a in node.names if a.name == "emet_sdk"}
        elif isinstance(node, ast.ImportFrom) and node.module == "emet_sdk":
            names |= {a.asname or a.name: a.name for a in node.names if a.name in ("configure", "turn")}
    configures = turns = False
    endpoints: list[str | None] = []
    calls = sorted((n for n in ast.walk(tree) if isinstance(n, ast.Call)), key=lambda n: (n.lineno, n.col_offset))
    for node in calls:
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in modules:
            called = func.attr
        elif isinstance(func, ast.Name) and func.id in names:
            called = names[func.id]
        else:
            continue
        turns = turns or called == "turn"
        if called == "configure":
            configures = True
            endpoints.append(None)
            for keyword in node.keywords:
                value = keyword.value
                if keyword.arg is None:
                    endpoints[-1] = UNREADABLE  # **kwargs may carry an endpoint
                elif keyword.arg != "endpoint" or (isinstance(value, ast.Constant) and value.value is None):
                    continue
                elif isinstance(value, ast.Constant) and isinstance(value.value, str):
                    endpoints[-1] = value.value
                else:
                    endpoints[-1] = UNREADABLE
    # The SDK keeps the first configure(); with several that differ, check cannot know which runs first
    endpoint = endpoints[0] if len(set(endpoints)) == 1 else (UNREADABLE if endpoints else None)
    return _Scan(configures, turns, endpoint)


def _sdk_version(root: Path) -> tuple[tuple[int, ...], str] | None:
    """Returns the emet-sdk version the app locks or pins, and the file that says it."""
    for name, pattern in (("uv.lock", SDK_LOCKED), ("poetry.lock", SDK_LOCKED), ("pyproject.toml", SDK_PIN)):
        if found := pattern.search(_read(root / name)):
            return _version(found.group(1)), name
    for path in sorted(root.glob("requirements*.txt")):
        if found := SDK_PIN.search(_read(path)):
            return _version(found.group(1)), path.name
    return None


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split(".") if part.isdigit())


def _origin(value: Any) -> str | None:
    """Returns a recorded endpoint as an origin, "" when none is recorded, or None when the SDK would refuse it."""
    if not isinstance(value, str) or not value:
        return ""
    try:
        return check_endpoint(value)
    except ValueError:
        return None


def _login_command(endpoint: str) -> str:
    return "uvx emet-cli login" if endpoint == DEFAULT_ENDPOINT else f"uvx emet-cli login --endpoint '{endpoint}'"


def _relogin(endpoint: str) -> str:
    """The fix for a refused key; a local key must be checked against the Emet that issued it."""
    if endpoint and endpoint != DEFAULT_ENDPOINT:
        login = _login_command(endpoint)
        return f"{ASK} `{login}` again with a current key from that Emet: a key works only on the Emet that issued it."
    return f"{ASK} `{_login_command(DEFAULT_ENDPOINT)}` again with a current key."


def _is_test(root: Path, path: Path) -> bool:
    relative = path.relative_to(root)
    name = relative.name
    tests = ("tests", "test")
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
        or any(p in tests for p in relative.parts)
    )


def _f(ok: bool, what: str, fix: str = "") -> dict[str, Any]:
    return {"ok": ok, "what": what, "fix": fix}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
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
