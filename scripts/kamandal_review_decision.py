#!/usr/bin/env python3
"""Translate Jarvis/Telegram Kamandal review replies into deterministic CLI actions."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def default_kamandal_root() -> Path:
    return Path(os.environ.get("KAMANDAL_ROOT") or Path(__file__).resolve().parents[1]).expanduser()


def kamandal_command(root: Path) -> list[str]:
    venv_bin = root / ".venv" / "bin" / "kamandal"
    if venv_bin.exists():
        return [str(venv_bin)]
    return [sys.executable, "-m", "kamandal_v2.cli"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message", required=True, help="Full visible Telegram/Jarvis callback or reply text")
    parser.add_argument("--source", default="jarvis")
    parser.add_argument("--decided-by", default="Suman")
    parser.add_argument("--kamandal-root", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = Path(args.kamandal_root).expanduser() if args.kamandal_root else default_kamandal_root()
    command = [
        *kamandal_command(root),
        "operator-review-decision-from-message",
        "--message",
        args.message,
        "--source",
        args.source,
        "--decided-by",
        args.decided_by,
    ]
    if args.dry_run:
        print(json.dumps({"dry_run": True, "cwd": str(root), "command": command}, indent=2))
        return 0
    completed = subprocess.run(command, cwd=str(root), check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
