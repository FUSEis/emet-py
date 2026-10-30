"""The files emet-cli reads and writes in the app's folder."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path

ENV_FILE = ".env"
EMET_DIR = ".emet"
KEY_FILE = f"{EMET_DIR}/key.json"
DELIVERY_FILE = f"{EMET_DIR}/last-delivery.json"
PROJECT_MARKERS = ("pyproject.toml", "requirements.txt", "requirements.in", "setup.py", "setup.cfg")
SKIP_DIRS = frozenset({".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".emet", "build", "dist", ".tox"})


def is_python_project(root: Path) -> bool:
    """Returns True when the folder holds a Python project definition."""
    return any((root / name).is_file() for name in PROJECT_MARKERS) or any(root.glob("requirements*.txt"))


def tracked(root: Path, paths: Iterable[str]) -> list[str]:
    """Returns the given paths that git tracks in the folder; outside a git repository, none."""
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "ls-files", "--", *paths],  # noqa: S607
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in out.stdout.splitlines() if line] if out.returncode == 0 else []


def write_private(path: Path, text: str) -> None:
    """Replaces a file atomically, readable only by its owner."""
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def set_env_line(path: Path, name: str, value: str) -> None:
    """Sets NAME=value in a dotenv file, leaving every other line as it was."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    entry = f"{name}={value}"
    kept = [line for line in lines if not _assigns(line, name)]
    write_private(path, "\n".join([*kept, entry]) + "\n")


def has_env_value(path: Path, name: str) -> bool:
    """Returns True when the dotenv file assigns a non-empty value to name."""
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        if _assigns(line, name) and line.split("=", 1)[1].strip().strip("'\""):
            return True
    return False


def ensure_ignored(path: Path, entries: Iterable[str]) -> list[str]:
    """Appends the entries an ignore file lacks; returns the ones added."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    present = {line.strip() for line in lines}
    missing = [e for e in entries if e not in present and e.rstrip("/") not in present]
    if missing:
        text = "\n".join(lines).rstrip("\n")
        path.write_text((f"{text}\n" if text else "") + "\n".join(missing) + "\n", encoding="utf-8")
    return missing


def python_files(root: Path) -> Iterable[Path]:
    """Yields the app's own Python files, skipping virtual environments and build output."""
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = [d for d in subdirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if name.endswith(".py"):
                yield Path(directory) / name


def _assigns(line: str, name: str) -> bool:
    stripped = line.strip().removeprefix("export ").lstrip()
    return stripped.split("=", 1)[0].strip() == name and "=" in stripped
