# -*- coding: utf-8 -*-
"""Command-line entry point.

    python -m integration_service.cli --provider all
    python -m integration_service.cli --provider github --dry-run
    python -m integration_service.cli --provision
    python -m integration_service.cli --check

Exit codes: ``0`` every run succeeded, ``1`` at least one run was partial,
``2`` at least one run failed outright, ``3`` the service could not start.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Dict, List, Optional, Type

from .config import get_settings
from .connectors.base import BaseConnector, ConnectorContext, build_context
from .connectors.frankfurter_connector import FrankfurterConnector
from .connectors.github_connector import GitHubConnector
from .connectors.jsonplaceholder_connector import JsonPlaceholderConnector
from .connectors.nager_date_connector import NagerDateConnector
from .connectors.open_meteo_connector import OpenMeteoConnector
from .errors import IntegrationError
from .json2_proof import run_proof
from .provisioning import provision
from .sanitize import sanitize
from .scheduler import Scheduler
from .sync_result import STATUS_FAILED, STATUS_PARTIAL, SyncResult

CONNECTORS: Dict[str, Type[BaseConnector]] = {
    "github": GitHubConnector,
    "jsonplaceholder": JsonPlaceholderConnector,
    "frankfurter": FrankfurterConnector,
    "open_meteo": OpenMeteoConnector,
    "nager_date": NagerDateConnector,
}

#: Order matters: JSONPlaceholder seeds partners with coordinates that
#: Open-Meteo then forecasts against.
RUN_ORDER = ["github", "jsonplaceholder", "frankfurter", "open_meteo", "nager_date"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m integration_service.cli",
        description="Synchronise five external APIs into Odoo 19 via the JSON-2 API.",
    )
    parser.add_argument(
        "--provider", "-p", default="all",
        choices=["all", *RUN_ORDER],
        help="Which connector to run (default: all).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Perform no write anywhere; every mutation is logged as [MOCK WRITE].",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Override SYNC_SAMPLE_LIMIT for this run (0 = no limit).",
    )
    parser.add_argument(
        "--provision", action="store_true",
        help="Create any missing custom field on res.partner, then exit.",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Report connectivity and per-model access, then exit.",
    )
    parser.add_argument(
        "--proof", action="store_true",
        help="Prove Odoo JSON-2 CREATE/READ/UPDATE/DELETE on a scratch record, then exit.",
    )
    parser.add_argument(
        "--keep-proof-record", action="store_true",
        help="With --proof: leave the scratch record in Odoo instead of deleting it.",
    )
    parser.add_argument(
        "--schedule", action="store_true",
        help="Run on the configured schedule instead of once (see SCHEDULE_* env vars).",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="With --schedule: run only the providers currently due, then exit. "
             "This is the form an OS scheduler (cron / Task Scheduler) should call.",
    )
    parser.add_argument(
        "--show-schedule", action="store_true",
        help="Print the resolved schedule and exit without running anything.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON results on stdout.",
    )
    parser.add_argument(
        "--no-sync-log", action="store_true",
        help="Do not persist a sync-log record for this run.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable DEBUG logging.",
    )
    return parser


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def run_providers(ctx: ConnectorContext, providers: List[str], write_log: bool = True) -> List[SyncResult]:
    results: List[SyncResult] = []
    for name in providers:
        connector = CONNECTORS[name](ctx)
        results.append(connector.run(write_log=write_log))
    return results


def exit_code_for(results: List[SyncResult]) -> int:
    statuses = {r.status for r in results}
    if STATUS_FAILED in statuses:
        return 2
    if STATUS_PARTIAL in statuses:
        return 1
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)

    try:
        settings = get_settings()
        if args.dry_run:
            settings.dry_run = True
        if args.limit is not None:
            settings.sample_limit = args.limit
        ctx = build_context(settings=settings)
    except IntegrationError as exc:
        print(f"Startup failed: {sanitize(exc)}", file=sys.stderr)
        return 3

    if args.provision:
        report = provision(ctx.odoo, dry_run=settings.dry_run)
        print(json.dumps(report, indent=2))
        return 0

    if args.proof:
        outcome = run_proof(ctx.odoo, keep=args.keep_proof_record)
        if args.json:
            print(json.dumps(outcome, indent=2, default=str))
        return 0 if outcome["ok"] else 2

    if args.check:
        from .provisioning import check_access
        report = {
            "odoo_url": settings.odoo.url,
            "database": settings.odoo.database,
            "dry_run": settings.dry_run,
            "sample_limit": settings.sample_limit,
            "github_write_enabled": settings.github.can_write,
            "model_access": check_access(ctx.odoo),
        }
        print(json.dumps(report, indent=2))
        return 0

    providers = RUN_ORDER if args.provider == "all" else [args.provider]

    if args.show_schedule or args.schedule:
        scheduler = Scheduler(
            providers=providers,
            runner=lambda name: CONNECTORS[name](ctx).run(write_log=not args.no_sync_log),
            settings=settings,
            odoo=ctx.odoo,
        )
        if args.show_schedule:
            print(json.dumps(scheduler.describe(), indent=2, default=str))
            return 0
        if args.once:
            # One pass over whatever is due; intended for cron / Task Scheduler.
            results = scheduler.run_due()
        else:
            scheduler.install_signal_handlers()
            results = scheduler.run_forever()
        if not results:
            print("Nothing was due; no provider ran.", file=sys.stderr)
            return 0
    else:
        results = run_providers(ctx, providers, write_log=not args.no_sync_log)

    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2, default=str))
    else:
        print("\n" + "=" * 78)
        print("SYNC SUMMARY")
        print("=" * 78)
        for result in results:
            print("  " + result.summary_line())
        totals = {
            "created": sum(r.created for r in results),
            "updated": sum(r.updated for r in results),
            "skipped": sum(r.skipped for r in results),
            "failed": sum(r.failed for r in results),
        }
        print("-" * 78)
        print(
            f"  {'TOTAL':<16} {'':<8} created={totals['created']} updated={totals['updated']} "
            f"skipped={totals['skipped']} failed={totals['failed']}"
        )
        print("=" * 78)
        for result in results:
            if result.errors:
                print(f"\n{result.provider} errors:")
                for error in result.errors[:10]:
                    print(f"  - {error}")
            if result.mock_writes:
                print(f"\n{result.provider} mock writes:")
                for entry in result.mock_writes[:10]:
                    print(f"  - {entry}")

    return exit_code_for(results)


if __name__ == "__main__":
    sys.exit(main())
