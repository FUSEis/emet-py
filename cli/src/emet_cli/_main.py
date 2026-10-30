"""The emet-cli command line."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from emet_cli._check import check, render
from emet_cli._login import login
from emet_cli._version import VERSION
from emet_sdk._transport import DEFAULT_ENDPOINT


def main(argv: Sequence[str] | None = None) -> int:
    """Runs one emet-cli command and returns its exit code."""
    parser = argparse.ArgumentParser(prog="emet-cli", description="Set up Emet in this Python app.")
    parser.add_argument("--version", action="version", version=f"emet-cli {VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)
    login_parser = commands.add_parser("login", help="Save your Emet key to this app's .env. Run it yourself.")
    login_parser.add_argument(
        "--endpoint", default=DEFAULT_ENDPOINT, help="Emet's address; only localhost is allowed besides production"
    )
    check_parser = commands.add_parser("check", help="Report what is set up. Sends nothing.")
    check_parser.add_argument("--last", action="store_true", help="Also say whether the last turn was captured")
    check_parser.add_argument("--json", action="store_true", help="Print the report as JSON, for your coding agent")
    args = parser.parse_args(argv)
    root = Path.cwd()
    try:
        if args.command == "login":
            return login(root, args.endpoint)
        code, report = check(root, last=args.last)
        print(json.dumps(report, indent=2) if args.json else render(report))
        return code
    except ValueError as error:
        print(f"emet-cli: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
