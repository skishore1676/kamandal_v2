#!/usr/bin/env python3
"""Run local Tastytrade contract checks; network probes require explicit flags."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def _load_env_override(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"environment file not found: {path}")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def _open_ticket(args: argparse.Namespace) -> dict:
    return {
        "order_id": "sandbox-contract-open",
        "underlying": args.underlying.upper(),
        "intent_type": "open",
        "limit_price": f"-{abs(args.credit):.2f}",
        "legs": [
            {
                "role": "short_put",
                "side": "sell",
                "effect": "open",
                "option_type": "put",
                "strike": args.put_strike,
                "expiration": args.expiration,
                "quantity": 1,
            },
            {
                "role": "short_call",
                "side": "sell",
                "effect": "open",
                "option_type": "call",
                "strike": args.call_strike,
                "expiration": args.expiration,
                "quantity": 1,
            },
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env.tastytrade-sandbox")
    parser.add_argument("--authenticate", action="store_true", help="Use OAuth to read account state and option-chain inventory")
    parser.add_argument("--dry-run-open", action="store_true", help="Send one broker dry-run; never submits an order")
    parser.add_argument("--underlying", default="QQQ")
    parser.add_argument("--expiration", default="")
    parser.add_argument("--put-strike", type=float, default=0.0)
    parser.add_argument("--call-strike", type=float, default=0.0)
    parser.add_argument("--credit", type=float, default=1.00)
    args = parser.parse_args()
    _load_env_override(Path(args.env_file).expanduser().resolve())

    from kamandal_v2.config import load_control
    from kamandal_v2.market.tastytrade import TastytradeAdapter

    adapter = TastytradeAdapter(load_control())
    result = {
        "configuration": adapter.configuration_report(),
        "order_contract_matrix": adapter.order_contract_matrix(),
        "authenticated": False,
        "dry_run_sent": False,
        "live_order_submitted": False,
    }
    if args.dry_run_open and not args.authenticate:
        raise SystemExit("--dry-run-open requires --authenticate")
    if args.authenticate:
        account = adapter.account_state()
        inventory = adapter.option_chain_inventory(args.underlying)
        result.update({
            "authenticated": True,
            "account": {
                "account_size": account.account_size,
                "buying_power": account.buying_power,
                "bpr_used": account.bpr_used,
                "positions_count": account.positions_count,
            },
            "option_chain_inventory": {
                key: inventory.get(key)
                for key in ("symbol", "underlying_count", "expirations", "strikes", "streamer_symbols")
            },
        })
    if args.dry_run_open:
        if not args.expiration or args.put_strike <= 0 or args.call_strike <= 0:
            raise SystemExit("--expiration, --put-strike, and --call-strike are required for --dry-run-open")
        ticket = _open_ticket(args)
        preflight = adapter.preflight_ticket(ticket)
        result["dry_run_sent"] = True
        result["dry_run"] = {
            "ok": preflight.ok,
            "bpr": preflight.bpr,
            "message": preflight.message,
            "underlying": ticket["underlying"],
            "expiration": args.expiration,
            "put_strike": args.put_strike,
            "call_strike": args.call_strike,
        }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
