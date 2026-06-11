"""Command line entrypoint."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from kamandal_v2.config import load_control
from kamandal_v2.domain.models import Candidate, Greeks, OptionLeg, Plan, PortfolioState, PreflightResult
from kamandal_v2.events.earnings import EarningsStore, capture_earnings_snapshots, earnings_event_status
from kamandal_v2.intelligence.llm_extractor import extract_ideas_llm
from kamandal_v2.intelligence.reviewer import review_rejections
from kamandal_v2.intelligence.transcripts import fetch_youtube_channel_videos, fetch_youtube_transcript, import_transcripts, scrape_youtube_smoke
from kamandal_v2.intelligence.x_bookmarks import import_x_bookmarks
from kamandal_v2.intelligence.x_digest import import_x_digest
from kamandal_v2.live.approval import (
    approve_live_request,
    expire_live_approval_requests,
    live_approval_status,
    reject_live_request,
    send_pending_live_approval_requests,
)
from kamandal_v2.live.advisory import run_live_advisory_plan
from kamandal_v2.live.book import format_live_book, live_book_sheet_rows, run_live_book
from kamandal_v2.live.health import format_live_health, run_live_health
from kamandal_v2.live.execution import cleanup_live_approvals, execute_live_approved, record_manual_live_fill, sync_live_orders
from kamandal_v2.live.management import run_live_management_plan
from kamandal_v2.live.operator_review import (
    OperatorReviewError,
    apply_operator_review_decision,
    operator_review_decision_from_message,
    send_pending_operator_review_requests,
)
from kamandal_v2.live.orders import build_open_ticket
from kamandal_v2.live.order_reconciliation import reconcile_live_orders
from kamandal_v2.live.reconciliation import reconcile_live_positions
from kamandal_v2.management.shadow import manage_shadow_positions, mark_shadow_portfolio, write_shadow_eod_report
from kamandal_v2.market.public import PublicAdapter
from kamandal_v2.market.fixture import FixtureMarketDataProvider
from kamandal_v2.market.tastytrade import TastytradeAdapter
from kamandal_v2.paths import resolve_path
from kamandal_v2.planner.config_loader import load_planner_config
from kamandal_v2.planner.config_validator import validate_config
from kamandal_v2.planner.engine import run_plan, run_shadow_cycle
from kamandal_v2.reports.go_live_audit import build_go_live_audit_report
from kamandal_v2.seed import build_seed_tables, seed_headers
from kamandal_v2.schemas import DAILY_PLAN_HEADER, LIVE_BOOK_HEADER
from kamandal_v2.sheets import bootstrap_sheet, pull_sheet_tables, write_daily_plan, write_live_book
from kamandal_v2.stores.sqlite import LocalStore
from kamandal_v2.volatility.iv import capture_iv_snapshots
from kamandal_v2.volatility.iv_store import IvStore


def main() -> None:
    parser = argparse.ArgumentParser(prog="kamandal")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("seed-preview", help="Print generated sheet seed sizes")
    subparsers.add_parser("bootstrap-sheet", help="Rewrite headers and seed rows in the configured Google Sheet")
    subparsers.add_parser("pull-sheet", help="Read configured Google Sheet tabs and cache the row payload")
    validate_parser = subparsers.add_parser("validate-config", help="Validate universe and playbook configuration")
    validate_parser.add_argument("--config-source", choices=["sheet", "seed"], default="sheet")

    plan_parser = subparsers.add_parser("plan", help="Build deterministic portfolio plans from local ideas")
    _add_planner_args(plan_parser)

    write_parser = subparsers.add_parser("write-daily-plan", help="Write the latest audited daily plan rows to Google Sheets")
    write_parser.add_argument("--latest-run", default="data/audit/latest_plan_run.json", help="Audit JSON produced by `kamandal plan`")

    shadow_parser = subparsers.add_parser("run-shadow-cycle", help="Build plans and auto-approve the top shadow plan when configured")
    _add_planner_args(shadow_parser)
    live_advisory_parser = subparsers.add_parser("live-advisory-plan", help="Build strict live advisory plans and optional sheet rows")
    _add_planner_args(live_advisory_parser)
    live_execute_parser = subparsers.add_parser("execute-live-approved", help="Execute sheet-approved live opening orders")
    live_execute_parser.add_argument("--submit", action="store_true", help="Submit real orders; default is dry-run")
    live_execute_parser.add_argument("--submit-auto", action="store_true", help="Submit only when global live submit and live.auto_submit_entries are enabled")
    live_close_execute_parser = subparsers.add_parser("execute-live-approved-closes", help="Execute sheet-approved live close orders")
    live_close_execute_parser.add_argument("--submit", action="store_true", help="Submit real close orders; default is dry-run")
    live_close_execute_parser.add_argument("--submit-auto", action="store_true", help="Submit only when global live submit and live.auto_submit_exits are enabled")
    live_health_parser = subparsers.add_parser("live-health", help="Print concise live book health")
    live_health_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    live_health_parser.add_argument(
        "--stale-close-order-minutes",
        type=int,
        default=None,
        help="Override close-order stale threshold when reporting; unset uses config/default",
    )
    live_book_parser = subparsers.add_parser("live-book", help="Print per-position live book cockpit")
    live_book_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    live_book_parser.add_argument("--write-sheet", action="store_true", help="Write current live book rows to the live_book sheet tab")
    sync_live_parser = subparsers.add_parser("sync-live-orders", help="Poll broker order status for submitted and cancel-pending live orders")
    sync_live_parser.add_argument("--read-only", action="store_true", help="Poll and record broker statuses without entry reprice/expiry actions")
    subparsers.add_parser("cleanup-live-approvals", help="Clear stale live approval cells after submit/fill/failure")
    approve_request_parser = subparsers.add_parser("approve-live-request", help="Approve a pending Telegram live request and update daily_plan")
    approve_request_parser.add_argument("--request-id", required=True)
    approve_request_parser.add_argument("--source", default="manual")
    approve_request_parser.add_argument("--approved-by", default="Suman")
    reject_request_parser = subparsers.add_parser("reject-live-request", help="Reject a pending Telegram live request")
    reject_request_parser.add_argument("--request-id", required=True)
    reject_request_parser.add_argument("--reason", required=True)
    reject_request_parser.add_argument("--source", default="manual")
    reject_request_parser.add_argument("--rejected-by", default="Suman")
    subparsers.add_parser("send-live-approval-requests", help="Send unsent pending Telegram live approval requests")
    subparsers.add_parser("expire-live-approval-requests", help="Expire stale pending Telegram live requests")
    subparsers.add_parser("live-approval-status", help="Show live Telegram approval request status")
    manual_fill_parser = subparsers.add_parser("record-manual-live-fill", help="Record a manually filled live order ticket")
    manual_fill_parser.add_argument("--ticket-hash", required=True)
    live_manage_parser = subparsers.add_parser("live-management-plan", help="Build strict live close advisory rows")
    live_manage_parser.add_argument("--config-source", choices=["sheet", "seed"], default="sheet")
    live_manage_parser.add_argument("--write-sheet", action="store_true")
    reconcile_parser = subparsers.add_parser("reconcile-live-positions", help="Compare broker live positions against Kamandal live ledger")
    reconcile_parser.add_argument("--write-sheet", action="store_true")
    reconcile_parser.add_argument("--send-review", action="store_true")
    reconcile_parser.add_argument("--dry-run", action="store_true")
    reconcile_orders_parser = subparsers.add_parser("reconcile-live-orders", help="Reconcile broker/local live order lifecycle state")
    reconcile_orders_parser.add_argument("--write-sheet", action="store_true")
    reconcile_orders_parser.add_argument("--dry-run", action="store_true")
    reconcile_orders_parser.add_argument(
        "--expire-stale-close-approvals",
        action="store_true",
        help="Apply stale local close-approval expiry; default reports only unless config enables it",
    )
    subparsers.add_parser("send-operator-review-requests", help="Send unsent reusable operator review requests")
    apply_review_parser = subparsers.add_parser("apply-operator-review-decision", help="Apply one deterministic operator review action")
    apply_review_parser.add_argument("--request-id", required=True)
    apply_review_parser.add_argument("--action", required=True)
    apply_review_parser.add_argument("--note", default="")
    apply_review_parser.add_argument("--source", default="manual")
    apply_review_parser.add_argument("--decided-by", default="Suman")
    message_review_parser = subparsers.add_parser("operator-review-decision-from-message", help="Parse Jarvis/Telegram text and apply a review action")
    message_review_parser.add_argument("--message", required=True)
    message_review_parser.add_argument("--source", default="telegram")
    message_review_parser.add_argument("--decided-by", default="Suman")
    compare_parser = subparsers.add_parser("compare-market-data", help="Compare chain/IV data between two configured providers")
    compare_parser.add_argument("--symbols", nargs="+", required=True)
    compare_parser.add_argument("--provider-a", choices=["public", "tastytrade", "fixture"], default="public")
    compare_parser.add_argument("--provider-b", choices=["public", "tastytrade", "fixture"], default="tastytrade")

    import_parser = subparsers.add_parser("import-transcripts", help="Import local transcripts into digest and rough idea YAML")
    import_parser.add_argument("--source-dir", default="data/transcripts")
    import_parser.add_argument("--digest-dir", default="data/digest")
    import_parser.add_argument("--ideas-dir", default="data/ideas")
    import_parser.add_argument("--config-source", choices=["sheet", "seed"], default="seed")
    import_parser.add_argument("--filter-universe", action="store_true", help="Drop extracted tickers outside configured universe")

    llm_extract_parser = subparsers.add_parser("extract-ideas-llm", help="Use Codex CLI to extract thesis ideas from local transcripts")
    llm_extract_parser.add_argument("--source-dir", default="data/transcripts")
    llm_extract_parser.add_argument("--digest-dir", default="data/digest")
    llm_extract_parser.add_argument("--ideas-dir", default="data/ideas")
    llm_extract_parser.add_argument("--output-prefix", default="llm_imported")
    llm_extract_parser.add_argument("--config-source", choices=["sheet", "seed"], default="sheet")
    llm_extract_parser.add_argument("--filter-universe", action="store_true", help="Drop extracted tickers outside configured universe")

    x_bookmarks_parser = subparsers.add_parser("import-x-bookmarks", help="Import sanitized X bookmark exports as LLM source docs")
    x_bookmarks_parser.add_argument("--source-file", default="", help="Sanitized public-export/normalized JSON; defaults to latest OpenClaw state")
    x_bookmarks_parser.add_argument("--latest-state", default="", help="OpenClaw x_bookmark_shadow latest.json")
    x_bookmarks_parser.add_argument("--trial-root", default="", help="Birdclaw trial root containing sanitized exports")
    x_bookmarks_parser.add_argument("--output-dir", default="data/source_docs/x_bookmarks")
    x_bookmarks_parser.add_argument("--digest-dir", default="data/digest/x_bookmarks")
    x_bookmarks_parser.add_argument("--limit", type=int, default=50)
    x_bookmarks_parser.add_argument("--config-source", choices=["sheet", "seed"], default="sheet")
    x_bookmarks_parser.add_argument("--filter-universe", action="store_true", help="Report configured universe symbol hits")

    x_digest_parser = subparsers.add_parser("import-x-digest", help="Import Birdclaw SQLite X digest posts as LLM source docs")
    x_digest_parser.add_argument("--db-path", default="", help="Birdclaw x_digest SQLite DB; defaults to canonical_store in latest state")
    x_digest_parser.add_argument("--latest-state", default="", help="OpenClaw x_daily_digest latest.json")
    x_digest_parser.add_argument("--trial-root", default="", help="Birdclaw trial root used to resolve relative DB paths")
    x_digest_parser.add_argument("--sources", default="", help="Comma-separated source lanes to import")
    x_digest_parser.add_argument("--output-dir", default="data/source_docs/x_digest")
    x_digest_parser.add_argument("--digest-dir", default="data/digest/x_digest")
    x_digest_parser.add_argument("--limit", type=int, default=0, help="Max records per source lane")
    x_digest_parser.add_argument("--since-hours", type=int, default=0)
    x_digest_parser.add_argument("--include-resurfaced", action="store_true", help="Include posts seen in previous digest runs")
    x_digest_parser.add_argument("--config-source", choices=["sheet", "seed"], default="sheet")
    x_digest_parser.add_argument("--filter-universe", action="store_true", help="Report configured universe symbol hits")

    cycle_parser = subparsers.add_parser("run-intelligence-cycle", help="Import transcripts, build Public/fixture plan, and optionally write daily_plan")
    cycle_parser.add_argument("--source-dir", default="data/transcripts/archive/youtube")
    cycle_parser.add_argument("--digest-dir", default="data/digest")
    cycle_parser.add_argument("--ideas-dir", default="data/ideas")
    cycle_parser.add_argument("--config-source", choices=["sheet", "seed"], default="sheet")
    cycle_parser.add_argument("--provider", choices=["fixture", "public"], default="public")
    cycle_parser.add_argument("--no-write-sheet", action="store_true", help="Do not write daily_plan")

    llm_cycle_parser = subparsers.add_parser("run-llm-cycle", help="LLM extract transcripts, optionally capture IV, then run deterministic shadow cycle")
    llm_cycle_parser.add_argument("--source-dir", default="data/transcripts/archive/youtube")
    llm_cycle_parser.add_argument("--digest-dir", default="data/digest")
    llm_cycle_parser.add_argument("--ideas-dir", default="data/ideas")
    llm_cycle_parser.add_argument("--config-source", choices=["sheet", "seed"], default="sheet")
    llm_cycle_parser.add_argument("--provider", choices=["fixture", "public"], default="public")
    llm_cycle_parser.add_argument("--no-write-sheet", action="store_true", help="Do not write daily_plan")
    llm_cycle_parser.add_argument("--skip-iv-capture", action="store_true", help="Skip IV capture for extracted symbols")
    llm_cycle_parser.add_argument("--output-prefix", default="llm_imported")

    review_parser = subparsers.add_parser("review-rejections", help="Use Codex CLI to review rejected candidates and propose local changes")
    review_parser.add_argument("--latest-run", default="data/audit/latest_plan_run.json")
    review_parser.add_argument("--ideas", default="")
    review_parser.add_argument("--output-dir", default="data/reviews")

    smoke_parser = subparsers.add_parser("public-smoke", help="Fetch Public account, chain, and preflight one defined-risk spread")
    smoke_parser.add_argument("--symbol", default="TSLA")
    live_smoke_parser = subparsers.add_parser("public-live-dry-run", help="Fetch Public account, preflight, and build live submit payload without submitting")
    live_smoke_parser.add_argument("--symbol", default="TSLA")
    tasty_smoke_parser = subparsers.add_parser("tastytrade-smoke", help="Fetch tastytrade OAuth/account state without submitting orders")
    tasty_smoke_parser.add_argument("--include-market-metrics", action="store_true", help="Also fetch IV metrics for the symbol")
    tasty_smoke_parser.add_argument("--symbol", default="TSLA")

    iv_parser = subparsers.add_parser("capture-iv", help="Capture current option-chain IV snapshots for universe symbols")
    iv_parser.add_argument("--symbols", nargs="*", help="Optional symbols; defaults to enabled sheet/seed universe")
    iv_parser.add_argument("--config-source", choices=["sheet", "seed"], default="sheet")
    iv_parser.add_argument("--provider", choices=["fixture", "public"], default="public")

    iv_status_parser = subparsers.add_parser("iv-status", help="Show latest locally stored IV and percentile")
    iv_status_parser.add_argument("--symbols", nargs="*", help="Optional symbols; defaults to enabled sheet universe")
    iv_status_parser.add_argument("--config-source", choices=["sheet", "seed"], default="sheet")

    earnings_parser = subparsers.add_parser("capture-earnings", help="Capture next earnings dates for universe symbols")
    earnings_parser.add_argument("--symbols", nargs="*", help="Optional symbols; defaults to enabled sheet universe")
    earnings_parser.add_argument("--config-source", choices=["sheet", "seed"], default="sheet")
    earnings_parser.add_argument("--provider", choices=["yfinance", "fixture"], default="yfinance")

    earnings_status_parser = subparsers.add_parser("earnings-status", help="Show locally stored earnings event status")
    earnings_status_parser.add_argument("--symbols", nargs="*", help="Optional symbols; defaults to enabled sheet universe")
    earnings_status_parser.add_argument("--config-source", choices=["sheet", "seed"], default="sheet")

    subparsers.add_parser("mark-shadow-portfolio", help="Mark open shadow fills from latest stored option-chain midpoint quotes")
    manage_shadow_parser = subparsers.add_parser("manage-shadow-positions", help="Apply shadow close rules to open positions")
    manage_shadow_parser.add_argument("--config-source", choices=["sheet", "seed"], default="sheet")
    manage_shadow_parser.add_argument("--dry-run", action="store_true")
    eod_parser = subparsers.add_parser("shadow-eod-report", help="Write a local shadow end-of-day report")
    eod_parser.add_argument("--config-source", choices=["sheet", "seed"], default="sheet")
    eod_parser.add_argument("--output-dir", default="data/reports/eod")
    audit_parser = subparsers.add_parser("go-live-audit-report", help="Write a two-day idea/plan/shadow quality audit report")
    audit_parser.add_argument("--dates", nargs="*", default=[], help="Optional YYYY-MM-DD dates; defaults to one active and one quiet day")
    audit_parser.add_argument("--since", default="", help="Start date YYYY-MM-DD for full-range triage")
    audit_parser.add_argument("--until", default="", help="End date YYYY-MM-DD for full-range triage")
    audit_parser.add_argument("--db", default="data/kamandal_v2.db")
    audit_parser.add_argument("--output-dir", default="data/reports/go_live_audit")

    youtube_parser = subparsers.add_parser("scrape-youtube-smoke", help="Fetch captions for one YouTube video and archive locally")
    youtube_parser.add_argument("--video-id", required=True)
    youtube_parser.add_argument("--transcript-dir", default="data/transcripts")
    youtube_parser.add_argument("--languages", default="en", help="Comma-separated language preference list")
    fetch_youtube_parser = subparsers.add_parser("fetch-youtube-transcript", help="Fetch captions for one YouTube video without extracting ideas")
    fetch_youtube_parser.add_argument("--video-id", required=True)
    fetch_youtube_parser.add_argument("--transcript-dir", default="data/transcripts")
    fetch_youtube_parser.add_argument("--languages", default="en", help="Comma-separated language preference list")
    fetch_youtube_parser.add_argument("--provider", choices=["yt_dlp", "api"], default="yt_dlp")
    fetch_youtube_parser.add_argument("--sleep-requests", type=float, default=3.0)
    fetch_youtube_parser.add_argument("--sleep-subtitles", type=float, default=5.0)
    fetch_youtube_parser.add_argument("--cookies-from-browser", default="")
    fetch_youtube_parser.add_argument("--archive-file", default="data/youtube_archive.txt")
    list_youtube_parser = subparsers.add_parser("list-youtube-channel-videos", help="List recent video IDs from configured YouTube channel RSS feeds")
    list_youtube_parser.add_argument("--channel-id", action="append", default=[], help="YouTube channel ID; repeat for multiple channels")
    list_youtube_parser.add_argument("--limit", type=int, default=1, help="Selected videos per channel")
    list_youtube_parser.add_argument("--scan-limit", type=int, default=20, help="Recent feed entries to evaluate per channel before selecting")
    list_youtube_parser.add_argument("--published-date", default="", help="Only select videos published on this local YYYY-MM-DD date")
    list_youtube_parser.add_argument("--timezone", default="America/Chicago", help="Timezone for --published-date")
    list_youtube_parser.add_argument("--min-score", type=int, default=None, help="Minimum title idea score when scoring is enabled")
    list_youtube_parser.add_argument("--no-score-titles", action="store_true", help="Keep feed order instead of ranking by title idea score")
    list_youtube_parser.add_argument("--include-keywords", default="", help="Comma-separated title include regex/substring filters")
    list_youtube_parser.add_argument("--exclude-keywords", default="", help="Comma-separated title exclude regex/substring filters")
    list_youtube_parser.add_argument("--output", default="", help="Optional file to write one video ID per line")

    args = parser.parse_args()

    config = load_control()
    seeds = build_seed_tables(config)
    if args.command == "seed-preview":
        print(json.dumps({key: len(value) for key, value in seeds.items()}, indent=2))
        return
    if args.command == "bootstrap-sheet":
        result = bootstrap_sheet(config, headers=seed_headers(), seed_tables=seeds)
        print(json.dumps({"spreadsheet_id": result.spreadsheet_id, "tabs": result.tabs}, indent=2))
        return
    if args.command == "pull-sheet":
        tables = pull_sheet_tables(config)
        cache_dir = resolve_path(((config.get("google_sheets") or {}).get("cache_dir") or "data/sheet_cache"))
        cache_dir.mkdir(parents=True, exist_ok=True)
        output = cache_dir / "latest_pull.json"
        output.write_text(json.dumps(tables, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps({"output": str(output), "rows": {key: len(value) for key, value in tables.items()}}, indent=2))
        return
    if args.command == "validate-config":
        universe, playbooks = load_planner_config(config, source=args.config_source)
        result = validate_config(universe, playbooks)
        print(json.dumps(result.to_dict(), indent=2))
        if not result.ok:
            raise SystemExit(1)
        return
    if args.command == "plan":
        result = run_plan(
            config,
            idea_paths=_expand_paths(args.ideas),
            config_source=args.config_source,
            provider=args.provider,
            write_sheet=args.write_sheet,
        )
        print(_plan_result_json(result))
        return
    if args.command == "write-daily-plan":
        latest_path = resolve_path(args.latest_run)
        payload = json.loads(latest_path.read_text(encoding="utf-8"))
        rows = payload.get("daily_plan_rows") or []
        count = write_daily_plan(config, rows, DAILY_PLAN_HEADER)
        print(json.dumps({"written_rows": count, "source": str(latest_path)}, indent=2))
        return
    if args.command == "run-shadow-cycle":
        result = run_shadow_cycle(
            config,
            idea_paths=_expand_paths(args.ideas),
            config_source=args.config_source,
            provider=args.provider,
            write_sheet=args.write_sheet,
        )
        print(_plan_result_json(result))
        return
    if args.command == "live-advisory-plan":
        result = run_live_advisory_plan(
            config,
            idea_paths=_expand_paths(args.ideas),
            config_source=args.config_source,
            provider=args.provider,
            write_sheet=args.write_sheet,
            persist_order_intents=args.write_sheet,
        )
        print(_plan_result_json(result))
        return
    if args.command == "execute-live-approved":
        print(json.dumps(execute_live_approved(config, submit=_live_submit_requested(config, args, close=False)), indent=2))
        return
    if args.command == "execute-live-approved-closes":
        print(json.dumps(execute_live_approved(config, submit=_live_submit_requested(config, args, close=True), close=True), indent=2))
        return
    if args.command == "live-health":
        report = run_live_health(
            LocalStore(),
            config,
            stale_close_order_minutes=args.stale_close_order_minutes,
        )
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(format_live_health(report))
        return
    if args.command == "live-book":
        report = run_live_book(LocalStore(), config)
        if args.write_sheet:
            report["sheet_rows_written"] = write_live_book(
                config,
                LIVE_BOOK_HEADER,
                live_book_sheet_rows(report, LIVE_BOOK_HEADER),
            )
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(format_live_book(report))
        return
    if args.command == "sync-live-orders":
        print(json.dumps(sync_live_orders(config, manage_entries=not args.read_only), indent=2))
        return
    if args.command == "cleanup-live-approvals":
        print(json.dumps(cleanup_live_approvals(config), indent=2))
        return
    if args.command == "approve-live-request":
        print(json.dumps(approve_live_request(config, args.request_id, source=args.source, approved_by=args.approved_by), indent=2))
        return
    if args.command == "reject-live-request":
        print(json.dumps(reject_live_request(args.request_id, reason=args.reason, source=args.source, rejected_by=args.rejected_by), indent=2))
        return
    if args.command == "send-live-approval-requests":
        print(json.dumps(send_pending_live_approval_requests(config), indent=2))
        return
    if args.command == "expire-live-approval-requests":
        print(json.dumps(expire_live_approval_requests(), indent=2))
        return
    if args.command == "live-approval-status":
        print(json.dumps(live_approval_status(), indent=2))
        return
    if args.command == "record-manual-live-fill":
        print(json.dumps(record_manual_live_fill(args.ticket_hash), indent=2))
        return
    if args.command == "live-management-plan":
        print(json.dumps(run_live_management_plan(config, config_source=args.config_source, write_sheet=args.write_sheet), indent=2))
        return
    if args.command == "reconcile-live-positions":
        print(json.dumps(reconcile_live_positions(config, write_sheet=args.write_sheet, send_review=args.send_review, dry_run=args.dry_run), indent=2))
        return
    if args.command == "reconcile-live-orders":
        print(
            json.dumps(
                reconcile_live_orders(
                    config,
                    write_sheet=args.write_sheet,
                    dry_run=args.dry_run,
                    expire_stale_close_approvals=True if args.expire_stale_close_approvals else None,
                ),
                indent=2,
            )
        )
        return
    if args.command == "send-operator-review-requests":
        print(json.dumps(send_pending_operator_review_requests(config), indent=2))
        return
    if args.command == "apply-operator-review-decision":
        print(json.dumps(apply_operator_review_decision(config, args.request_id, args.action, note=args.note, source=args.source, decided_by=args.decided_by), indent=2))
        return
    if args.command == "operator-review-decision-from-message":
        try:
            print(json.dumps(operator_review_decision_from_message(config, args.message, source=args.source, decided_by=args.decided_by), indent=2))
        except OperatorReviewError as exc:
            print(json.dumps({"status": "rejected", "reason": str(exc)}, indent=2))
            raise SystemExit(1) from exc
        return
    if args.command == "compare-market-data":
        print(json.dumps(_compare_market_data(config, args.symbols, args.provider_a, args.provider_b), indent=2))
        return
    if args.command == "import-transcripts":
        result = import_transcripts(
            args.source_dir,
            digest_dir=args.digest_dir,
            ideas_dir=args.ideas_dir,
            output_prefix=args.output_prefix,
            allowed_symbols=_universe_symbols(config, args.config_source) if args.filter_universe else None,
        )
        print(json.dumps(result.to_dict(), indent=2))
        return
    if args.command == "extract-ideas-llm":
        result = extract_ideas_llm(
            config,
            args.source_dir,
            digest_dir=args.digest_dir,
            ideas_dir=args.ideas_dir,
            output_prefix=args.output_prefix,
            allowed_symbols=_universe_symbols(config, args.config_source) if args.filter_universe else None,
        )
        print(json.dumps(result.to_dict(), indent=2))
        return
    if args.command == "import-x-bookmarks":
        x_config = ((config.get("source_intelligence") or {}).get("x_bookmarks") or {})
        result = import_x_bookmarks(
            source_file=args.source_file or None,
            latest_state=args.latest_state or x_config.get("latest_state_file") or "~/.openclaw/workspace-main/state/x_bookmark_shadow/latest.json",
            trial_root=args.trial_root or x_config.get("trial_root") or "~/Documents/birdclaw",
            output_dir=args.output_dir,
            digest_dir=args.digest_dir,
            limit=args.limit,
            allowed_symbols=_universe_symbols(config, args.config_source) if args.filter_universe else None,
        )
        print(json.dumps(result.to_dict(), indent=2))
        return
    if args.command == "import-x-digest":
        x_config = ((config.get("source_intelligence") or {}).get("x_digest") or {})
        result = import_x_digest(
            db_path=args.db_path or x_config.get("db_path") or None,
            latest_state=args.latest_state or x_config.get("latest_state_file") or "~/.openclaw/workspace-main/state/x_daily_digest/latest.json",
            trial_root=args.trial_root or x_config.get("trial_root") or "~/Documents/birdclaw",
            sources=_csv(args.sources or x_config.get("sources") or "bookmarks,timeline"),
            output_dir=args.output_dir,
            digest_dir=args.digest_dir,
            limit=args.limit or int(x_config.get("limit") or 50),
            since_hours=args.since_hours or int(x_config.get("since_hours") or 96),
            include_resurfaced=args.include_resurfaced,
            allowed_symbols=_universe_symbols(config, args.config_source) if args.filter_universe else None,
        )
        print(json.dumps(result.to_dict(), indent=2))
        return
    if args.command == "run-intelligence-cycle":
        import_result = import_transcripts(
            args.source_dir,
            digest_dir=args.digest_dir,
            ideas_dir=args.ideas_dir,
            output_prefix=args.output_prefix,
            allowed_symbols=_universe_symbols(config, args.config_source),
        )
        plan_result = None
        if import_result.ideas_path is not None:
            plan_result = run_shadow_cycle(
                config,
                idea_paths=[import_result.ideas_path],
                config_source=args.config_source,
                provider=args.provider,
                write_sheet=not args.no_write_sheet,
            )
        print(json.dumps({
            "import": import_result.to_dict(),
            "plan": json.loads(_plan_result_json(plan_result)) if plan_result else None,
        }, indent=2))
        return
    if args.command == "run-llm-cycle":
        extraction_result = extract_ideas_llm(
            config,
            args.source_dir,
            digest_dir=args.digest_dir,
            ideas_dir=args.ideas_dir,
            output_prefix=args.output_prefix,
            allowed_symbols=_universe_symbols(config, args.config_source),
        )
        iv_result = None
        plan_result = None
        if extraction_result.ideas_path is not None:
            symbols = _idea_symbols_from_file(extraction_result.ideas_path)
            if args.provider == "public" and not args.skip_iv_capture and symbols:
                iv_result = capture_iv_snapshots(
                    config,
                    symbols=symbols,
                    config_source=args.config_source,
                    provider=args.provider,
                )
            plan_result = run_shadow_cycle(
                config,
                idea_paths=[extraction_result.ideas_path],
                config_source=args.config_source,
                provider=args.provider,
                write_sheet=not args.no_write_sheet,
            )
        print(json.dumps({
            "extraction": extraction_result.to_dict(),
            "iv": iv_result.to_dict() if iv_result else None,
            "plan": json.loads(_plan_result_json(plan_result)) if plan_result else None,
        }, indent=2))
        return
    if args.command == "review-rejections":
        result = review_rejections(
            config,
            latest_run=args.latest_run,
            ideas_path=args.ideas or None,
            output_dir=args.output_dir,
        )
        print(json.dumps(result.to_dict(), indent=2))
        return
    if args.command == "public-smoke":
        print(json.dumps(_public_smoke(config, args.symbol), indent=2))
        return
    if args.command == "public-live-dry-run":
        print(json.dumps(_public_live_dry_run(config, args.symbol), indent=2))
        return
    if args.command == "tastytrade-smoke":
        print(json.dumps(_tastytrade_smoke(config, args.symbol, include_market_metrics=args.include_market_metrics), indent=2))
        return
    if args.command == "capture-iv":
        result = capture_iv_snapshots(
            config,
            symbols=[symbol.upper() for symbol in args.symbols] if args.symbols else None,
            config_source=args.config_source,
            provider=args.provider,
        )
        print(json.dumps(_iv_capture_json(result), indent=2))
        return
    if args.command == "iv-status":
        symbols = [symbol.upper() for symbol in args.symbols] if args.symbols else sorted(_universe_symbols(config, args.config_source))
        print(json.dumps(_iv_status_json(symbols), indent=2))
        return
    if args.command == "capture-earnings":
        result = capture_earnings_snapshots(
            config,
            symbols=[symbol.upper() for symbol in args.symbols] if args.symbols else None,
            config_source=args.config_source,
            provider=args.provider,
        )
        print(json.dumps(result.to_dict(), indent=2))
        return
    if args.command == "earnings-status":
        symbols = [symbol.upper() for symbol in args.symbols] if args.symbols else sorted(_universe_symbols(config, args.config_source))
        print(json.dumps(_earnings_status_json(symbols), indent=2))
        return
    if args.command == "mark-shadow-portfolio":
        print(json.dumps(mark_shadow_portfolio(), indent=2))
        return
    if args.command == "manage-shadow-positions":
        print(json.dumps(manage_shadow_positions(config, config_source=args.config_source, dry_run=args.dry_run).to_dict(), indent=2))
        return
    if args.command == "shadow-eod-report":
        print(json.dumps(write_shadow_eod_report(config, config_source=args.config_source, output_dir=args.output_dir), indent=2))
        return
    if args.command == "go-live-audit-report":
        result = build_go_live_audit_report(
            sqlite_path=args.db,
            output_dir=args.output_dir,
            dates=args.dates or None,
            since=args.since or None,
            until=args.until or None,
        )
        print(json.dumps(result.to_dict(), indent=2))
        return
    if args.command == "scrape-youtube-smoke":
        transcript = scrape_youtube_smoke(
            args.video_id,
            transcript_dir=args.transcript_dir,
            languages=[item.strip() for item in args.languages.split(",") if item.strip()],
        )
        result = import_transcripts(args.transcript_dir)
        print(json.dumps({"transcript_path": str(transcript), "import": result.to_dict()}, indent=2))
        return
    if args.command == "fetch-youtube-transcript":
        transcript = fetch_youtube_transcript(
            args.video_id,
            transcript_dir=args.transcript_dir,
            languages=[item.strip() for item in args.languages.split(",") if item.strip()],
            provider=args.provider,
            sleep_requests=args.sleep_requests,
            sleep_subtitles=args.sleep_subtitles,
            cookies_from_browser=args.cookies_from_browser,
            archive_file=args.archive_file,
        )
        print(json.dumps({"transcript_path": str(transcript)}, indent=2))
        return
    if args.command == "list-youtube-channel-videos":
        videos = fetch_youtube_channel_videos(
            args.channel_id,
            limit=args.limit,
            scan_limit=args.scan_limit,
            published_date=args.published_date,
            timezone=args.timezone,
            score_titles=not args.no_score_titles,
            min_score=args.min_score,
            include_keywords=_csv(args.include_keywords),
            exclude_keywords=_csv(args.exclude_keywords),
        )
        if args.output:
            output_path = resolve_path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("\n".join(video.video_id for video in videos) + ("\n" if videos else ""), encoding="utf-8")
        print(json.dumps({"videos": [video.to_dict() for video in videos], "output": args.output or None}, indent=2))
        return


def _add_planner_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ideas", nargs="+", default=["data/ideas"], help="Idea YAML/JSON files, directories, or glob patterns")
    parser.add_argument("--config-source", choices=["sheet", "seed"], default="sheet")
    parser.add_argument("--provider", choices=["fixture", "public"], default="fixture")
    parser.add_argument("--write-sheet", action="store_true", help="Write generated plan rows to daily_plan")


def _live_submit_requested(config: dict[str, Any], args: argparse.Namespace, *, close: bool) -> bool:
    if bool(getattr(args, "submit", False)):
        return True
    if not bool(getattr(args, "submit_auto", False)):
        return False
    if not _truthy(os.environ.get("KAMANDAL_LIVE_SUBMIT")):
        return False
    key = "auto_submit_exits" if close else "auto_submit_entries"
    return _truthy((config.get("live") or {}).get(key))


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _expand_paths(values: list[str]) -> list[Path]:
    paths: list[Path] = []
    for value in values:
        matches = sorted(resolve_path(".").glob(value)) if any(ch in value for ch in "*?[") else []
        if matches:
            paths.extend(matches)
        else:
            paths.append(resolve_path(value))
    return paths


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _plan_result_json(result: object) -> str:
    plans = getattr(result, "plans")
    return json.dumps(
        {
            "plan_run_id": getattr(result, "plan_run_id"),
            "ideas": len(getattr(result, "ideas")),
            "candidates": len(getattr(result, "candidates")),
            "plans": len(plans),
            "metrics": getattr(result, "metrics"),
            "top_plan": plans[0].to_dict() if plans else None,
        },
        indent=2,
    )


def _universe_symbols(config: dict, source: str) -> set[str]:
    universe, _playbooks = load_planner_config(config, source=source)
    return {entry.symbol for entry in universe if entry.enabled}


def _idea_symbols_from_file(path: Path) -> list[str]:
    import yaml

    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    symbols = []
    for idea in payload.get("ideas") or []:
        symbol = str((idea or {}).get("underlying") or "").upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _public_smoke(config: dict, symbol: str) -> dict:
    adapter = PublicAdapter(config)
    account = adapter.account_state()
    snapshot = adapter.chain_snapshot(symbol)
    calls = [
        quote for quote in snapshot.quotes
        if quote.option_type == "call" and 0.18 <= abs(quote.delta) <= 0.30
    ]
    calls = sorted(calls, key=lambda quote: (abs(abs(quote.delta) - 0.22), quote.dte, quote.strike))
    short = calls[0]
    long_choices = [
        quote for quote in snapshot.quotes
        if quote.option_type == "call"
        and quote.expiration == short.expiration
        and quote.strike > short.strike
    ]
    if not long_choices:
        raise RuntimeError(f"No Public long call found above {short.strike} for {symbol}")
    long = sorted(long_choices, key=lambda quote: quote.strike)[0]
    legs = [
        OptionLeg.from_quote(short, role="short_call", side="sell"),
        OptionLeg.from_quote(long, role="long_call", side="buy"),
    ]
    greeks = Greeks()
    for leg in legs:
        greeks = greeks + leg.signed_greeks
    net_credit = round(sum(leg.signed_mid for leg in legs), 4)
    candidate = Candidate(
        candidate_id=f"public_smoke_{symbol.upper()}",
        idea_id="public_smoke",
        underlying=symbol.upper(),
        playbook_id="call_spread",
        structure="call_spread",
        legs=legs,
        net_credit=net_credit,
        estimated_bpr=max(abs(legs[1].strike - legs[0].strike) * 100 - max(net_credit, 0) * 100, 1.0),
        greeks=greeks,
        liquidity_score=1.0,
        score=0.0,
        reasons=["public_smoke"],
    )
    preflight = adapter.preflight(candidate)
    return {
        "symbol": symbol.upper(),
        "account": {
            "account_size": account.account_size,
            "buying_power": account.buying_power,
            "positions_count": account.positions_count,
        },
        "chain": {
            "source": snapshot.source,
            "underlying_price": snapshot.underlying_price,
            "quotes": len(snapshot.quotes),
        },
        "candidate": candidate.to_dict(),
        "preflight": preflight.to_dict(),
        "live_order_submitted": False,
    }


def _public_live_dry_run(config: dict, symbol: str) -> dict:
    smoke = _public_smoke(config, symbol)
    candidate = _candidate_from_smoke(smoke["candidate"])
    portfolio = PortfolioState(account_size=smoke["account"]["account_size"], buying_power=smoke["account"]["buying_power"], bpr_used=0, positions_count=0)
    plan = Plan(
        plan_id="public_live_dry_run",
        plan_rank=1,
        status="eligible",
        candidates=[candidate],
        score=0,
        total_bpr=candidate.estimated_bpr,
        bpr_utilization_pct=0,
        buying_power_after=portfolio.buying_power - candidate.estimated_bpr,
        portfolio_before=portfolio,
        portfolio_after=portfolio,
    )
    candidate.preflight = PreflightResult(
        ok=bool(smoke["preflight"]["ok"]),
        bpr=float(smoke["preflight"]["bpr"]),
        message=str(smoke["preflight"].get("message") or ""),
        raw=dict(smoke["preflight"].get("raw") or {}),
    )
    ticket = build_open_ticket(plan, candidate)
    return {**smoke, "order_ticket": ticket, "live_order_submitted": False}


def _tastytrade_smoke(config: dict, symbol: str, *, include_market_metrics: bool = False) -> dict:
    adapter = TastytradeAdapter(config)
    account = adapter.account_state()
    result: dict[str, Any] = {
        "broker": "tastytrade",
        "available": adapter.available(),
        "account": {
            "account_size": account.account_size,
            "buying_power": account.buying_power,
            "bpr_used": account.bpr_used,
            "positions_count": account.positions_count,
        },
    }
    if include_market_metrics:
        result["market_metrics"] = {
            "symbol": symbol.upper(),
            "iv_rank": adapter.iv_rank(symbol),
            "iv_percentile": adapter.iv_percentile(symbol),
            "iv_abs": adapter.iv_abs(symbol),
        }
    return result


def _compare_market_data(config: dict, symbols: list[str], provider_a: str, provider_b: str) -> dict:
    adapters = {
        provider_a: _market_compare_adapter(config, provider_a),
        provider_b: _market_compare_adapter(config, provider_b),
    }
    rows = []
    for symbol in [item.upper() for item in symbols]:
        row = {"symbol": symbol, provider_a: _provider_market_summary(adapters[provider_a], symbol), provider_b: _provider_market_summary(adapters[provider_b], symbol)}
        row["diff"] = _market_summary_diff(row[provider_a], row[provider_b])
        rows.append(row)
    return {"provider_a": provider_a, "provider_b": provider_b, "symbols": rows, "execution_broker_changed": False}


def _market_compare_adapter(config: dict, provider: str) -> object:
    if provider == "public":
        return PublicAdapter(config)
    if provider == "tastytrade":
        return TastytradeAdapter(config)
    if provider == "fixture":
        return FixtureMarketDataProvider()
    raise ValueError(f"unsupported market data provider: {provider}")


def _provider_market_summary(adapter: object, symbol: str) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "available": bool(adapter.available()) if hasattr(adapter, "available") else True,
        "iv_percentile": _safe_call(adapter, "iv_percentile", symbol),
        "iv_rank": _safe_call(adapter, "iv_rank", symbol),
        "iv_abs": _safe_call(adapter, "iv_abs", symbol),
    }
    try:
        snapshot = adapter.chain_snapshot(symbol)
        quotes = list(snapshot.quotes)
        summary.update({
            "chain_status": "ok",
            "underlying_price": snapshot.underlying_price,
            "quotes": len(quotes),
            "min_open_interest": min((quote.open_interest for quote in quotes), default=None),
            "median_open_interest": _median([quote.open_interest for quote in quotes]),
            "median_spread_pct": _median([quote.spread_pct for quote in quotes]),
            "source": snapshot.source,
        })
    except Exception as exc:  # noqa: BLE001
        summary.update({"chain_status": "error", "chain_error": str(exc)})
        if hasattr(adapter, "option_chain_inventory"):
            try:
                summary["option_chain_inventory"] = adapter.option_chain_inventory(symbol)
            except Exception as inventory_exc:  # noqa: BLE001
                summary["option_chain_inventory_error"] = str(inventory_exc)
    return summary


def _market_summary_diff(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    diff: dict[str, Any] = {}
    for key in ("iv_percentile", "iv_rank", "iv_abs", "underlying_price", "median_open_interest", "median_spread_pct"):
        if isinstance(left.get(key), (int, float)) and isinstance(right.get(key), (int, float)):
            diff[f"{key}_delta"] = round(float(left[key]) - float(right[key]), 6)
    if left.get("chain_status") != right.get("chain_status"):
        diff["chain_status_mismatch"] = [left.get("chain_status"), right.get("chain_status")]
    return diff


def _safe_call(adapter: object, name: str, *args: Any) -> Any:
    if not hasattr(adapter, name):
        return None
    try:
        return getattr(adapter, name)(*args)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _median(values: list[float | int]) -> float | None:
    values = sorted(float(value) for value in values if value is not None)
    if not values:
        return None
    midpoint = len(values) // 2
    if len(values) % 2:
        return round(values[midpoint], 6)
    return round((values[midpoint - 1] + values[midpoint]) / 2.0, 6)


def _candidate_from_smoke(payload: dict) -> Candidate:
    return Candidate(
        candidate_id=str(payload["candidate_id"]),
        idea_id=str(payload["idea_id"]),
        underlying=str(payload["underlying"]),
        playbook_id=str(payload["playbook_id"]),
        structure=str(payload["structure"]),
        legs=[OptionLeg(**leg) for leg in payload["legs"]],
        net_credit=float(payload["net_credit"]),
        estimated_bpr=float(payload["estimated_bpr"]),
        greeks=Greeks(**payload["greeks"]),
        liquidity_score=float(payload["liquidity_score"]),
        score=float(payload["score"]),
        reasons=list(payload.get("reasons") or []),
        rejection_reason=str(payload.get("rejection_reason") or ""),
    )


def _iv_capture_json(result: object) -> dict:
    store = IvStore()
    snapshots = getattr(result, "snapshots")
    return {
        "snapshots": [
            {
                **snapshot.to_dict(),
                "iv_abs": snapshot.iv,
                "history_count": len(store.history(snapshot.symbol, metric=snapshot.metric)),
                "iv_percentile": store.percentile(snapshot.symbol, metric=snapshot.metric),
                "iv_rank": store.rank(snapshot.symbol, metric=snapshot.metric),
            }
            for snapshot in snapshots
        ],
        "failures": getattr(result, "failures"),
    }


def _iv_status_json(symbols: list[str]) -> dict:
    store = IvStore()
    rows = []
    for symbol in symbols:
        latest = store.latest(symbol)
        if latest is None:
            rows.append({"symbol": symbol, "status": "missing"})
            continue
        rows.append({
            "symbol": symbol,
            "status": "ok",
            "latest": latest.to_dict(),
            "iv_abs": latest.iv,
            "history_count": len(store.history(symbol, metric=latest.metric)),
            "iv_percentile": store.percentile(symbol, metric=latest.metric),
            "iv_rank": store.rank(symbol, metric=latest.metric),
        })
    return {"symbols": rows}


def _earnings_status_json(symbols: list[str]) -> dict:
    store = EarningsStore()
    rows = []
    for symbol in symbols:
        latest = store.latest(symbol)
        if latest is None:
            rows.append({"symbol": symbol, "status": "missing", "event_status": "unknown"})
            continue
        rows.append({
            "symbol": symbol,
            "status": "ok",
            "latest": latest.to_dict(),
            "next_earnings_date": latest.next_earnings_date,
            "event_status": earnings_event_status(latest),
        })
    return {"symbols": rows}


if __name__ == "__main__":
    main()
