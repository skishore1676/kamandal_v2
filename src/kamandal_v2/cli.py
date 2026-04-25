"""Command line entrypoint."""

from __future__ import annotations

import argparse
import json

from kamandal_v2.config import load_control
from kamandal_v2.seed import build_seed_tables, seed_headers
from kamandal_v2.sheets import bootstrap_sheet


def main() -> None:
    parser = argparse.ArgumentParser(prog="kamandal")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("seed-preview", help="Print generated sheet seed sizes")
    subparsers.add_parser("bootstrap-sheet", help="Rewrite headers and seed rows in the configured Google Sheet")
    args = parser.parse_args()

    config = load_control()
    seeds = build_seed_tables(config)
    if args.command == "seed-preview":
        print(json.dumps({key: len(value) for key, value in seeds.items()}, indent=2))
        return
    if args.command == "bootstrap-sheet":
        result = bootstrap_sheet(config, headers=seed_headers(), seed_tables=seeds)
        print(json.dumps({"spreadsheet_id": result.spreadsheet_id, "tabs": result.tabs}, indent=2))


if __name__ == "__main__":
    main()

