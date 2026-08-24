#!/usr/bin/env python3
"""Atomically update Tastytrade runtime settings without echoing credentials."""

from __future__ import annotations

import argparse
import getpass
import os
import tempfile
from pathlib import Path


PRODUCTION_API = "https://api.tastyworks.com"
SANDBOX_API = "https://api.cert.tastyworks.com"
ORDERS_API_VERSION = "20260427"


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _replace_values(lines: list[str], updates: dict[str, str]) -> list[str]:
    written: set[str] = set()
    result = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key not in updates:
            result.append(line)
            continue
        if key not in written:
            result.append(f"{key}={updates[key]}")
            written.add(key)
    if result and result[-1] != "":
        result.append("")
    result.extend(f"{key}={value}" for key, value in updates.items() if key not in written)
    return result


def _atomic_write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines).rstrip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--sandbox", action="store_true")
    parser.add_argument(
        "--rotate-oauth",
        action="store_true",
        help="Prompt for and replace client secret and refresh token",
    )
    args = parser.parse_args()

    path = Path(args.env_file).expanduser().resolve()
    account_number = getpass.getpass("Tastytrade account number (input hidden): ").strip()
    if not account_number:
        raise SystemExit("account number is required; no file changed")
    updates = {
        "TASTYTRADE_ACCOUNT_NUMBER": account_number,
        "TASTYTRADE_API_BASE_URL": SANDBOX_API if args.sandbox else PRODUCTION_API,
        "TASTYTRADE_ORDERS_API_VERSION": ORDERS_API_VERSION,
    }
    if args.rotate_oauth:
        client_secret = getpass.getpass("New Tastytrade client secret (input hidden): ").strip()
        refresh_token = getpass.getpass("New Tastytrade refresh token (input hidden): ").strip()
        if not client_secret or not refresh_token:
            raise SystemExit("client secret and refresh token are required; no file changed")
        updates.update({
            "TASTYTRADE_CLIENT_SECRET": client_secret,
            "TASTYTRADE_REFRESH_TOKEN": refresh_token,
        })
    _atomic_write(path, _replace_values(_read_lines(path), updates))
    print({"updated": sorted(updates), "env_file": str(path), "permissions": "0600", "values_printed": False})


if __name__ == "__main__":
    main()
