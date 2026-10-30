"""emet-cli login: the one command that handles the key, run by the developer in their own terminal."""

from __future__ import annotations

import getpass
import http.client
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from emet_cli._project import (
    DELIVERY_FILE,
    EMET_DIR,
    ENV_FILE,
    KEY_FILE,
    ensure_ignored,
    is_python_project,
    set_env_line,
    tracked,
    write_private,
)
from emet_sdk._configure import KEY_VARIABLE
from emet_sdk._transport import TRACES_PATH, Post, check_endpoint, urllib_post
from emet_sdk._version import VERSION as SDK_VERSION

CHECK_TIMEOUT = 10.0
NOT_A_TERMINAL = (
    "emet-cli login reads your key from the keyboard, so it only runs in a terminal. "
    "Run it yourself in a separate terminal, in your app's folder, not through your coding agent."
)


@dataclass
class Io:
    """What login needs from the outside world, swappable in tests."""

    out: Callable[[str], None] = print
    ask_secret: Callable[[str], str] = getpass.getpass
    ask: Callable[[str], str] = input
    is_terminal: Callable[[], bool] = field(default=lambda: sys.stdin.isatty())
    post: Post = urllib_post


def login(root: Path, endpoint: str, io: Io | None = None) -> int:
    """Asks for the key, checks it with Emet without sending a trace, and saves it to the app's .env."""
    io = io or Io()
    origin = check_endpoint(endpoint)
    io.out(f"Setting up Emet for the app in {root}")
    if not is_python_project(root):
        io.out("This folder has no pyproject.toml or requirements file. Run emet-cli login in your app's folder.")
        return 1
    tracked_files = tracked(root, [ENV_FILE, EMET_DIR])
    if tracked_files:
        io.out(
            f"git tracks {', '.join(tracked_files)}, so the key would be committed. "
            f"Untrack it first: git rm --cached -r {' '.join(tracked_files)}"
        )
        return 1
    if not io.is_terminal():
        io.out(NOT_A_TERMINAL)
        return 1
    io.out("Use a key for a development Application. Create one in Emet under Applications if you have none.")
    key = io.ask_secret("Paste your Emet key (input is hidden): ").strip()
    prefix = _prefix(key)
    if prefix is None:
        io.out("That does not look like an Emet key, which reads <prefix>.<secret>. Nothing was saved.")
        return 1

    verdict = _check(io.post, origin, key)
    if verdict == "rejected":
        io.out(
            "Emet rejected this key: it is mistyped, revoked or expired, or its Application is inactive. "
            "Nothing was saved."
        )
        return 1
    verified = verdict == "accepted"
    if not verified:
        io.out(f"Could not check the key with Emet ({verdict}).")
        if io.ask("Save it anyway? [y/N] ").strip().lower() not in ("y", "yes"):
            io.out("Nothing was saved.")
            return 1

    set_env_line(root / ENV_FILE, KEY_VARIABLE, key)
    (root / EMET_DIR).mkdir(mode=0o700, exist_ok=True)
    record = {
        "prefix": prefix,
        "endpoint": origin,
        "verified": verified,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sdk_version": SDK_VERSION,
    }
    write_private(root / KEY_FILE, json.dumps(record, indent=2) + "\n")
    ignored = [ENV_FILE, f"{EMET_DIR}/"]
    added = ensure_ignored(root / ".gitignore", ignored)
    if (root / ".dockerignore").is_file():
        ensure_ignored(root / ".dockerignore", ignored)

    io.out(f"Saved {KEY_VARIABLE} to {ENV_FILE} (key {prefix}...).")
    if added:
        io.out(f"Added {', '.join(added)} to .gitignore.")
    io.out(f"Your app's delivery results will go to {DELIVERY_FILE}. Your coding agent can continue now.")
    return 0


def _prefix(key: str) -> str | None:
    prefix, dot, secret = key.partition(".")
    if not dot or not prefix or not secret or len(prefix) > 8 or any(c.isspace() for c in key):
        return None
    return prefix


def _check(post: Post, origin: str, key: str) -> str:
    # An empty envelope passes authentication first: a good key gets 400 for the body, a bad one 403
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"emet-cli/{SDK_VERSION}",
    }
    try:
        reply = post(origin + TRACES_PATH, headers, b"{}", CHECK_TIMEOUT)
    except TimeoutError:
        return "it did not answer in time"
    except (OSError, http.client.HTTPException):
        return "it could not be reached"
    if reply.status == 400:
        return "accepted"
    if reply.status in (401, 403):
        return "rejected"
    if reply.status == 429:
        return "rate limited, try again in a few minutes"
    return f"it answered {reply.status}"
