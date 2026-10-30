"""emet-cli init: installs emet-sdk and tells the coding agent exactly what to wire, without editing the app's code."""

from __future__ import annotations

import importlib.metadata
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from emet_cli._check import check
from emet_cli._project import DELIVERY_FILE, EMET_DIR, ENV_FILE, ensure_ignored, is_python_project, python_files
from emet_sdk._configure import KEY_VARIABLE

PYDANTIC_AI = re.compile(r"""(?m)^[^#\n]*["']?pydantic[-_]ai(?:[-_]slim)?\b""")
AGENT_CONSTRUCTED = re.compile(r"\bAgent\(")
AGENT_RUN = re.compile(r"\.(?:run|run_sync|run_stream|run_stream_events|iter)\(")
LOADS_DOTENV = re.compile(r"\bload_dotenv\(|\bdotenv_values\(|--env-file\b|\benv_file\s*=")
SETTINGS_ONLY = re.compile(r"\bpydantic_settings\b|\bBaseSettings\b")
INSTRUMENTED = re.compile(r"\binstrument_all\(|\binstrument\s*=\s*True|\bInstrumentation\(")
RUN_FILES = ("Procfile", "Makefile", "Dockerfile", "docker-compose.yml", "compose.yaml", "pyproject.toml", "justfile")
MANUAL_GUIDE = "https://github.com/FUSEis/emet-py#quick-start"

CONFIGURE_SNIPPET = """import emet_sdk

# Once at startup, after the app's own tracing setup
emet_sdk.configure(
    service_name="<your app's name>",
    on_delivery=emet_sdk.write_delivery_file(".emet/last-delivery.json"),
)
Agent.instrument_all()"""
TURN_SNIPPET = """# Values come from the server's own auth and storage, never the request body
with emet_sdk.turn(tenant_ref=<Team External ID>, conversation_id=<thread id>, user=<user's email>):
    result = await agent.run(...)"""
DOTENV_SNIPPET = """# The app does not load .env yet. Add python-dotenv and, at the very top of startup:
from dotenv import load_dotenv

load_dotenv()"""

Runner = Callable[[list[str], Path], int]


def run_command(command: list[str], cwd: Path) -> int:
    """Runs a package manager command in the app's folder and returns its exit code."""
    try:
        return subprocess.run(command, cwd=cwd, check=False, timeout=300).returncode  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        return 1


def init(root: Path, install: bool = True, runner: Runner = run_command) -> tuple[int, dict[str, Any]]:
    """Installs emet-sdk and returns the exit code and what the agent needs to wire it up."""
    if not is_python_project(root):
        return 1, {"supported": False, "reason": "no pyproject.toml or requirements file in this folder"}
    sources = {path: _read(path) for path in python_files(root)}
    dependency_text = "\n".join(_read(root / name) for name in ("pyproject.toml", *_requirements(root)))
    if not PYDANTIC_AI.search(dependency_text):
        return 1, {
            "supported": False,
            "reason": "emet-cli supports Pydantic AI apps today, and this app does not depend on pydantic-ai",
            "manual_guide": MANUAL_GUIDE,
        }

    version = importlib.metadata.version("emet-sdk")
    requirement = f"emet-sdk=={version}"
    manager, command = _install_command(root, requirement)
    if manager == "pip":
        _add_requirement(root, requirement)
    installed = None
    if install and command:
        installed = runner(command, root) == 0

    (root / EMET_DIR).mkdir(mode=0o700, exist_ok=True)
    ensure_ignored(root / ".gitignore", [ENV_FILE, f"{EMET_DIR}/"])

    run_text = "\n".join(_read(root / name) for name in RUN_FILES)
    loads_env = sorted(_rel(root, p) for p, text in sources.items() if LOADS_DOTENV.search(text))
    if LOADS_DOTENV.search(run_text):
        loads_env.append("a run command with --env-file")
    settings_only = not loads_env and any(SETTINGS_ONLY.search(text) for text in sources.values())
    _, setup = check(root, last=False)
    report: dict[str, Any] = {
        "supported": True,
        "package_manager": manager,
        "sdk_version": version,
        "install_command": " ".join(command) if command else f"pip install {requirement}, in the app's environment",
        "installed": installed,
        "key_saved": setup["setup"]["key_saved"],
        "env_loaded_by": loads_env,
        "agents_in": _matches(root, sources, AGENT_CONSTRUCTED),
        "agent_runs_at": _lines(root, sources, AGENT_RUN),
        "instrumented_in": _matches(root, sources, INSTRUMENTED),
        "configure_in": setup["setup"]["configure_in"],
        "turn_in": setup["setup"]["turn_in"],
        "delivery_file": DELIVERY_FILE,
        "snippets": {"configure": CONFIGURE_SNIPPET, "turn": TURN_SNIPPET},
        "next_steps": [],
    }
    steps: list[str] = report["next_steps"]
    if installed is False:
        steps.append(f"Installing failed. Run `{report['install_command']}` and read its error.")
    if not command:
        steps.append(f"Install {requirement} into the app's environment; it is now in the requirements file.")
    if not report["key_saved"]:
        steps.append(
            f"Ask the developer to run `uvx emet-cli login` in their own terminal; {KEY_VARIABLE} is not saved."
        )
    if not loads_env:
        report["snippets"]["dotenv"] = DOTENV_SNIPPET
        steps.append(
            f"Make the app load {ENV_FILE} at startup"
            + (" (pydantic-settings reads .env into its own fields, not the environment)" if settings_only else "")
            + ": use the dotenv snippet, or start it with `--env-file .env`."
        )
    if not report["configure_in"]:
        steps.append("Add the configure snippet where the app starts, once.")
    if not report["instrumented_in"]:
        steps.append("Call Agent.instrument_all() at startup so Pydantic AI emits spans.")
    if not report["turn_in"]:
        steps.append(
            "Wrap each agent run listed in agent_runs_at in emet_sdk.turn(). Propose the tenant_ref, conversation_id "
            "and user expressions from the app's own auth and storage, and confirm them with the developer first."
        )
    steps.append("Run `uvx emet-cli check`, then start the app and send one question.")
    return 0, report


def _install_command(root: Path, requirement: str) -> tuple[str, list[str]]:
    if (root / "uv.lock").is_file() or "[tool.uv]" in _read(root / "pyproject.toml"):
        return "uv", ["uv", "add", requirement]
    if (root / "poetry.lock").is_file():
        return "poetry", ["poetry", "add", requirement]
    if (root / "pyproject.toml").is_file() and "[project]" in _read(root / "pyproject.toml"):
        return "uv", ["uv", "add", requirement]
    # Only the app's own virtual environment; another interpreter would install it in the wrong place
    for venv in (".venv", "venv"):
        python = root / venv / ("Scripts/python.exe" if (root / venv / "Scripts").is_dir() else "bin/python")
        if python.is_file():
            return "pip", [str(python), "-m", "pip", "install", requirement]
    return "pip", []


def _add_requirement(root: Path, requirement: str) -> None:
    files = _requirements(root)
    if not files:
        return
    path = root / ("requirements.txt" if "requirements.txt" in files else files[0])
    text = _read(path)
    if re.search(r"(?mi)^\s*emet[-_]sdk\b", text):
        return
    path.write_text(text.rstrip("\n") + ("\n" if text.strip() else "") + requirement + "\n", encoding="utf-8")


def _requirements(root: Path) -> list[str]:
    return sorted(p.name for p in root.glob("requirements*.txt"))


def _matches(root: Path, sources: dict[Path, str], pattern: re.Pattern[str]) -> list[str]:
    return sorted(_rel(root, p) for p, text in sources.items() if pattern.search(text))


def _lines(root: Path, sources: dict[Path, str], pattern: re.Pattern[str]) -> list[str]:
    found: list[str] = []
    for path, text in sources.items():
        if not AGENT_CONSTRUCTED.search(text) and "agent" not in text.lower():
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if pattern.search(line) and "agent" in line.lower():
                found.append(f"{_rel(root, path)}:{number}")
    return sorted(found)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def render_init(report: dict[str, Any]) -> str:
    """Formats an init report for a person reading the terminal."""
    if not report.get("supported"):
        guide = f" See {report['manual_guide']}." if report.get("manual_guide") else ""
        return f"emet-cli init cannot set this app up: {report['reason']}.{guide}"
    lines = [f"emet-sdk {report['sdk_version']} via {report['package_manager']}: {report['install_command']}"]
    if report["agent_runs_at"]:
        lines.append(f"Agent runs: {', '.join(report['agent_runs_at'])}")
    lines.append("Next:")
    lines.extend(f"  - {step}" for step in report["next_steps"])
    return "\n".join(lines)
