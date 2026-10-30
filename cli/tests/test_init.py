from __future__ import annotations

import importlib.metadata
import json
import re
from pathlib import Path

import pytest

from emet_cli._init import init, render_init
from emet_cli._main import main
from emet_cli._version import VERSION

SDK = importlib.metadata.version("emet-sdk")
AGENT_APP = """from fastapi import FastAPI
from pydantic_ai import Agent

app = FastAPI()
agent = Agent("openai:gpt-5")


@app.post("/chat")
async def chat(body):
    result = await agent.run(body.message)
    return result.output
"""


class Runner:
    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], cwd: Path) -> int:
        self.commands.append(command)
        return self.code


@pytest.fixture
def app(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\ndependencies = ["pydantic-ai-slim[openai]>=2"]\n'
    )
    (tmp_path / "uv.lock").write_text("")
    (tmp_path / "main.py").write_text(AGENT_APP)
    return tmp_path


def test_a_uv_app_gets_the_matching_sdk_and_a_full_survey(app: Path) -> None:
    runner = Runner()
    code, report = init(app, runner=runner)
    assert code == 0
    assert runner.commands == [["uv", "add", f"emet-sdk=={SDK}"]]
    assert (report["package_manager"], report["installed"], report["agent_runs_at"]) == ("uv", True, ["main.py:10"])
    assert report["agents_in"] == ["main.py"]
    assert report["env_loaded_by"] == []
    steps = " ".join(report["next_steps"])
    for step in ("emet-cli login", "load .env", "configure snippet", "instrument_all", "Wrap each agent run"):
        assert step in steps
    assert "dotenv" in report["snippets"]
    # It edits no code, and keeps the key and results out of git
    assert (app / "main.py").read_text() == AGENT_APP
    assert (app / ".emet").is_dir()
    assert (app / ".gitignore").read_text() == ".env\n.emet/\n"


def test_a_wired_app_has_only_the_final_step_left(app: Path) -> None:
    (app / ".env").write_text("EMET_APPLICATION_KEY=AbCd1234.secret\n")
    (app / "main.py").write_text(
        "from dotenv import load_dotenv\nimport emet_sdk\nload_dotenv()\n"
        "emet_sdk.configure()\nAgent.instrument_all()\nwith emet_sdk.turn(t, c):\n    agent.run_sync('x')\n"
    )
    _, report = init(app, runner=Runner())
    assert report["env_loaded_by"] == ["main.py"]
    assert report["next_steps"] == ["Run `uvx emet-cli check`, then start the app and send one question."]
    assert "dotenv" not in report["snippets"]


def test_an_env_file_flag_in_the_run_command_counts_as_loading(app: Path) -> None:
    (app / "Procfile").write_text("web: uvicorn main:app --env-file .env\n")
    _, report = init(app, runner=Runner())
    assert report["env_loaded_by"] == ["a run command with --env-file"]


def test_pydantic_settings_alone_is_called_out(app: Path) -> None:
    (app / "settings.py").write_text("from pydantic_settings import BaseSettings\n")
    _, report = init(app, runner=Runner())
    assert "not the environment" in " ".join(report["next_steps"])


def test_poetry_is_used_when_the_app_uses_it(app: Path) -> None:
    (app / "uv.lock").unlink()
    (app / "poetry.lock").write_text("")
    (app / "pyproject.toml").write_text('[tool.poetry]\nname = "app"\n[tool.poetry.dependencies]\npydantic-ai = "*"\n')
    runner = Runner()
    _, report = init(app, runner=runner)
    assert (report["package_manager"], runner.commands) == ("poetry", [["poetry", "add", f"emet-sdk=={SDK}"]])


def test_a_pip_app_installs_into_its_own_venv_only(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("pydantic-ai\n")
    (tmp_path / "main.py").write_text(AGENT_APP)
    runner = Runner()
    _, report = init(tmp_path, runner=runner)
    # No virtual environment: nothing runs, the requirement is added and the agent installs it
    assert runner.commands == []
    assert (tmp_path / "requirements.txt").read_text() == f"pydantic-ai\nemet-sdk=={SDK}\n"
    assert "requirements file" in " ".join(report["next_steps"])
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    init(tmp_path, runner=runner)
    assert runner.commands == [[str(python), "-m", "pip", "install", f"emet-sdk=={SDK}"]]
    assert (tmp_path / "requirements.txt").read_text().count("emet-sdk") == 1


def test_a_failed_install_is_reported(app: Path) -> None:
    _, report = init(app, runner=Runner(code=1))
    assert report["installed"] is False
    assert "Installing failed" in report["next_steps"][0]


def test_no_install_reports_only(app: Path) -> None:
    runner = Runner()
    _, report = init(app, install=False, runner=runner)
    assert (runner.commands, report["installed"]) == ([], None)


@pytest.mark.parametrize(
    ("files", "reason"),
    [
        ({}, "no pyproject.toml"),
        ({"pyproject.toml": '[project]\nname = "app"\ndependencies = ["langchain"]\n'}, "Pydantic AI apps"),
    ],
)
def test_an_unsupported_app_is_told_so(tmp_path: Path, files: dict[str, str], reason: str) -> None:
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    code, report = init(tmp_path, runner=Runner())
    assert (code, report["supported"]) == (1, False)
    assert reason in report["reason"]
    assert "cannot set this app up" in render_init(report)


def test_the_command_line_prints_json(
    app: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(app)
    assert main(["init", "--no-install", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["sdk_version"] == SDK
    assert main(["init", "--no-install"]) == 0
    assert "Next:" in capsys.readouterr().out


def test_the_agent_setup_prompt_pins_this_cli_version() -> None:
    text = (Path(__file__).parents[1] / "AGENT_SETUP.md").read_text()
    pins = set(re.findall(r"emet-cli@([\w.]+)", text))
    assert pins == {VERSION}
