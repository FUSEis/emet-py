from __future__ import annotations

import json
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from emet_cli._login import Io, login
from emet_sdk._transport import Reply

KEY = "AbCd1234.not-a-real-secret-for-tests"


@dataclass
class Fake:
    """Stands in for the terminal and for Emet."""

    key: str = KEY
    status: int | BaseException = 400
    answer: str = ""
    terminal: bool = True
    lines: list[str] = field(default_factory=lambda: [])
    posts: list[tuple[str, dict[str, str], bytes]] = field(default_factory=lambda: [])

    def io(self) -> Io:
        return Io(
            out=self.lines.append,
            ask_secret=lambda prompt: self.key,
            ask=lambda prompt: self.answer,
            is_terminal=lambda: self.terminal,
            post=self.post,
        )

    def post(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> Reply:
        self.posts.append((url, headers, body))
        if isinstance(self.status, BaseException):
            raise self.status
        return Reply(self.status, {}, b"{}")

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def app(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    return tmp_path


def test_a_good_key_is_saved_and_never_printed(app: Path) -> None:
    (app / ".env").write_text("DATABASE_URL=postgres://localhost/app\nEMET_APPLICATION_KEY=old.key\n")
    fake = Fake()
    assert login(app, "https://app.emet.so", fake.io()) == 0
    assert (app / ".env").read_text() == f"DATABASE_URL=postgres://localhost/app\nEMET_APPLICATION_KEY={KEY}\n"
    record = json.loads((app / ".emet" / "key.json").read_text())
    assert (record["prefix"], record["verified"], record["endpoint"]) == ("AbCd1234", True, "https://app.emet.so")
    assert KEY not in (app / ".emet" / "key.json").read_text()
    assert ".env\n.emet/\n" in (app / ".gitignore").read_text()
    assert KEY not in fake.text
    assert "key AbCd1234..." in fake.text
    assert stat.S_IMODE((app / ".env").stat().st_mode) == 0o600


def test_the_check_is_an_empty_post_that_creates_nothing(app: Path) -> None:
    fake = Fake()
    login(app, "http://localhost:8000", fake.io())
    [(url, headers, body)] = fake.posts
    assert (url, body) == ("http://localhost:8000/api/v1/traces/", b"{}")
    assert headers["Authorization"] == f"Bearer {KEY}"


def test_a_rejected_key_is_not_saved(app: Path) -> None:
    fake = Fake(status=403)
    assert login(app, "https://app.emet.so", fake.io()) == 1
    assert not (app / ".env").exists()
    assert not (app / ".emet").exists()
    assert "rejected this key" in fake.text


@pytest.mark.parametrize(
    ("status", "said"),
    [
        (TimeoutError(), "did not answer in time"),
        (ConnectionRefusedError(), "could not be reached"),
        (429, "rate limited"),
        (502, "answered 502"),
    ],
)
def test_an_unchecked_key_is_saved_only_when_the_developer_says_so(
    app: Path, status: int | BaseException, said: str
) -> None:
    declined = Fake(status=status, answer="")
    assert login(app, "https://app.emet.so", declined.io()) == 1
    assert said in declined.text
    assert not (app / ".env").exists()
    accepted = Fake(status=status, answer="y")
    assert login(app, "https://app.emet.so", accepted.io()) == 0
    assert json.loads((app / ".emet" / "key.json").read_text())["verified"] is False


def test_it_refuses_without_a_terminal(app: Path) -> None:
    fake = Fake(terminal=False)
    assert login(app, "https://app.emet.so", fake.io()) == 1
    assert "only runs in a terminal" in fake.text
    assert fake.posts == []


def test_it_refuses_outside_a_python_project(tmp_path: Path) -> None:
    fake = Fake()
    assert login(tmp_path, "https://app.emet.so", fake.io()) == 1
    assert "no pyproject.toml" in fake.text


def test_it_refuses_when_git_tracks_the_env_file(app: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=app, check=True)
    (app / ".env").write_text("DEBUG=1\n")
    subprocess.run(["git", "add", ".env"], cwd=app, check=True)
    fake = Fake()
    assert login(app, "https://app.emet.so", fake.io()) == 1
    assert "git rm --cached -r .env" in fake.text
    assert (app / ".env").read_text() == "DEBUG=1\n"


def test_something_that_is_not_a_key_is_refused(app: Path) -> None:
    for bad in ("", "no-dot", ".secret", "toolongprefix.secret", "AbCd1234.with space"):
        fake = Fake(key=bad)
        assert login(app, "https://app.emet.so", fake.io()) == 1
        assert fake.posts == []


def test_an_endpoint_other_than_production_or_localhost_is_refused(app: Path) -> None:
    with pytest.raises(ValueError, match="localhost"):
        login(app, "https://example.com", Fake().io())


def test_an_existing_dockerignore_gets_the_entries_too(app: Path) -> None:
    (app / ".gitignore").write_text(".env\n")
    (app / ".dockerignore").write_text("node_modules\n")
    fake = Fake()
    assert login(app, "https://app.emet.so", fake.io()) == 0
    assert (app / ".gitignore").read_text() == ".env\n.emet/\n"
    assert (app / ".dockerignore").read_text() == "node_modules\n.env\n.emet/\n"
