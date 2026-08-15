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
from typing import Any, Dict, List, Optional, Type

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
from .sync_request import SyncRequestWorker
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
        "--drain-requests", action="store_true",
        help="Run every Sync Now request currently queued in Odoo, then exit. "
             "This is what turns the in-Odoo button into a real provider sync.",
    )
    parser.add_argument(
        "--serve-requests", action="store_true",
        help="Poll Odoo for Sync Now requests and fulfil them until interrupted.",
    )
    parser.add_argument(
        "--poll-seconds", type=int, default=10,
        help="With --serve-requests: seconds between queue polls (default 10).",
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
        "--test-graph", action="store_true",
        help="Test Microsoft 365 Graph API connection (GET /v1.0/users?$top=5), then exit.",
    )
    parser.add_argument(
        "--readiness", action="store_true",
        help="Run Microsoft 365 Tenant Readiness & Discovery, then exit.",
    )
    parser.add_argument(
        "--user-360", action="store_true",
        help="Run Microsoft 365 User 360 Diagnostic for a UPN/email.",
    )
    parser.add_argument(
        "--onboard", action="store_true",
        help="Run synthetic employee onboarding.",
    )
    parser.add_argument(
        "--remediate", action="store_true",
        help="Run helpdesk remediation action for a synthetic user.",
    )
    parser.add_argument(
        "--offboard", action="store_true",
        help="Run synthetic employee offboarding.",
    )
    parser.add_argument(
        "--upn", type=str, default="",
        help="Target UPN/email for User 360, Remediation, or Offboarding.",
    )
    parser.add_argument(
        "--action", type=str, default="",
        help="Remediation action (block_signin, unblock_signin, revoke_sessions, reset_password, add_group, remove_group, assign_license, remove_license).",
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Execute mutations after explicit approval (default is dry-run / plan).",
    )
    parser.add_argument(
        "--target-group-id", type=str, default="",
        help="Target approved group ID for remediation actions.",
    )
    parser.add_argument(
        "--target-sku-id", type=str, default="",
        help="Target approved SKU ID for remediation actions.",
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


def sync_graph_audit(ctx: ConnectorContext, client: Any, operation_label: str) -> Dict[str, Any]:
    """Persist captured Microsoft Graph audit events to the Odoo control plane."""
    try:
        audit_res = client.audit_logger.sync_to_odoo(ctx.odoo, operation_label=operation_label)
        print(f"Graph Audit Sync Status: {audit_res.get('status')} ({audit_res.get('written', audit_res.get('reason', 0))})")
        return audit_res
    except Exception as exc:
        print(f"Graph Audit Sync Failed: {sanitize(exc)}", file=sys.stderr)
        return {"status": "failed", "reason": sanitize(exc)}


def run_readiness(ctx: ConnectorContext) -> int:
    from .clients.ms_graph_client import MSGraphClient
    from .clients.tenant_context import TenantContext
    from .clients.token_provider import MSALTokenProvider
    from .tenant_readiness import TenantReadinessService

    print("==================================================================")
    print("MICROSOFT 365 TENANT READINESS & DISCOVERY REPORT")
    print("==================================================================")
    try:
        tenant_context = TenantContext.from_settings(ctx.settings.m365)
        token_provider = MSALTokenProvider(settings=ctx.settings.m365)
        client = MSGraphClient(tenant_context=tenant_context, token_provider=token_provider)
        service = TenantReadinessService(graph_client=client)

        report = service.run_readiness_check()

        print(f"Tenant ID:         {sanitize(report.tenant_id)}")
        print(f"Display Name:      {sanitize(report.display_name)}")
        print(f"Primary Domain:    {sanitize(report.primary_domain)}")
        print(f"Readiness Status:  {report.readiness_status.upper()}")
        print(f"Last Check Time:   {report.last_readiness_check}")
        
        print("\n--- DISCOVERED DOMAINS ---")
        print(f"Total Domains: {len(report.domains)}")
        for d in report.domains:
            def_flag = " (Default)" if d.get("is_default") else ""
            print(f"  - {sanitize(d.get('name'))}{def_flag} [{d.get('status')}]")

        print("\n--- SUBSCRIBED SKUs / LICENSE CAPACITY ---")
        print(f"Total License Capacity:     {report.total_license_capacity}")
        print(f"Consumed License Capacity:  {report.consumed_license_capacity}")
        print(f"Available License Capacity: {report.available_license_capacity}")
        for s in report.skus:
            print(
                f"  - SKU: {sanitize(s.get('sku_part_number'))} ({s.get('sku_id')}) | "
                f"Enabled={s.get('enabled_units')} Consumed={s.get('consumed_units')} Available={s.get('available_units')}"
            )

        print("\n--- CONFIGURED LAB TEST GROUPS ---")
        print(f"Discovered LAB Groups: {len(report.lab_groups)}")
        for g in report.lab_groups:
            print(f"  - Group: '{sanitize(g.get('name'))}' | ID={sanitize(g.get('graph_id'))} | Type={g.get('group_type')}")

        print("\n--- GRAPH API CAPABILITIES ---")
        for c in report.capabilities:
            print(f"  - {c.get('name'):<25}: {c.get('status').upper()} ({c.get('detail')})")

        print("\n--- ODOO ONLINE PERSISTENCE ---")
        sync_res = service.sync_to_odoo(ctx.odoo, report)
        print(f"Odoo Online Sync Status: {sync_res.get('status')} ({sync_res.get('reason', sync_res.get('model'))})")
        sync_graph_audit(ctx, client, "Tenant Readiness")

        print("==================================================================")
        return 0 if report.readiness_status in ("ready", "partial") else 2

    except IntegrationError as exc:
        print(f"Tenant Readiness Check Failed: {sanitize(exc)}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Unexpected Error: {sanitize(exc)}", file=sys.stderr)
        return 3


def run_user_360(ctx: ConnectorContext, upn: str) -> int:
    from .clients.ms_graph_client import MSGraphClient
    from .clients.tenant_context import TenantContext
    from .clients.token_provider import MSALTokenProvider
    from .user_360 import User360Service

    if not upn:
        print("Error: --user-360 requires --upn <user_upn_or_email>", file=sys.stderr)
        return 2

    print("==================================================================")
    print(f"MICROSOFT 365 USER 360 DIAGNOSTIC: {sanitize(upn)}")
    print("==================================================================")
    try:
        tenant_context = TenantContext.from_settings(ctx.settings.m365)
        token_provider = MSALTokenProvider(settings=ctx.settings.m365)
        client = MSGraphClient(tenant_context=tenant_context, token_provider=token_provider)
        service = User360Service(graph_client=client)

        snapshot = service.get_user_snapshot(upn)

        print(f"UPN:                {sanitize(snapshot.upn)}")
        print(f"Graph Object ID:    {sanitize(snapshot.graph_id)}")
        print(f"Display Name:       {sanitize(snapshot.display_name)}")
        print(f"Account Enabled:    {snapshot.account_enabled}")
        print(f"Job Title:          {sanitize(snapshot.job_title)}")
        print(f"Department:         {sanitize(snapshot.department)}")
        print(f"Usage Location:     {sanitize(snapshot.usage_location)}")
        print(f"Manager:            {snapshot.manager.get('displayName', 'Not assigned') if isinstance(snapshot.manager, dict) else snapshot.manager}")
        print(f"Assigned Licenses:  {len(snapshot.assigned_licenses)}")
        print(f"Direct Groups:      {len(snapshot.direct_groups)}")
        for g in snapshot.direct_groups:
            print(f"  - Group: '{sanitize(g.get('displayName'))}' ({g.get('id')})")
        print(f"Transitive Groups:  {len(snapshot.transitive_groups)}")
        for g in snapshot.transitive_groups:
            print(f"  - Transitive Group: '{sanitize(g.get('displayName'))}' ({g.get('id')})")
        if getattr(snapshot, "availability", None):
            for key, status in snapshot.availability.items():
                if status != "Available":
                    print(f"  - {key}: {sanitize(status)}")
        print(f"Auth Methods:       {snapshot.auth_methods}")
        if isinstance(snapshot.devices, dict):
            registered = snapshot.devices.get("registered")
            managed = snapshot.devices.get("managed")
            print(f"Registered Devices: {len(registered) if isinstance(registered, list) else registered}")
            print(f"Managed Devices:    {len(managed) if isinstance(managed, list) else managed}")
        else:
            print(f"Devices:            {snapshot.devices}")

        print("\n--- ODOO ONLINE PERSISTENCE ---")
        sync_res = service.sync_to_odoo(ctx.odoo, snapshot)
        print(f"Odoo Online Sync Status: {sync_res.get('status')}")
        sync_graph_audit(ctx, client, "User 360")
        print("==================================================================")
        return 0 if snapshot.diagnostic_status in ("success", "partial") else 2

    except Exception as exc:
        print(f"User 360 Diagnostic Failed: {sanitize(exc)}", file=sys.stderr)
        return 2


def run_onboarding(ctx: ConnectorContext, execute: bool) -> int:
    from .clients.ms_graph_client import MSGraphClient
    from .clients.tenant_context import TenantContext
    from .clients.token_provider import MSALTokenProvider
    from .onboarding import OnboardingService

    print("==================================================================")
    print(f"SYNTHETIC EMPLOYEE ONBOARDING ({'EXECUTE & VERIFY' if execute else 'PLAN / DRY RUN'})")
    print("==================================================================")
    try:
        tenant_context = TenantContext.from_settings(ctx.settings.m365)
        token_provider = MSALTokenProvider(settings=ctx.settings.m365)
        client = MSGraphClient(tenant_context=tenant_context, token_provider=token_provider)
        service = OnboardingService(graph_client=client)

        plan = service.plan_onboarding(
            first_name="TestWorker",
            last_name="Auto01",
            job_title="Cloud Engineer",
            department="Cloud Operations",
        )

        print(f"Planned Target UPN:     {sanitize(plan.upn)}")
        print(f"Planned Display Name:   {sanitize(plan.display_name)}")
        print(f"Planned Job Title:      {sanitize(plan.job_title)}")
        print(f"Target License SKU:     {sanitize(plan.target_sku_part_number or 'None available')}")
        print(f"Target LAB Groups:      {', '.join(plan.target_group_names)}")
        if plan.validation_errors:
            print("\nPlan Validation Errors:")
            for err in plan.validation_errors:
                print(f"  - {sanitize(err)}")
        print("\nPlanned Graph Mutations:")
        for mut in plan.planned_mutations:
            print(f"  Step {mut['step']}: {mut['action']} [{mut['method']} {mut['path']}]")

        if not execute:
            plan_result = service.plan_result(plan)
            sync_res = service.sync_to_odoo(ctx.odoo, plan_result)
            print("\n[DRY RUN COMPLETE] No Microsoft 365 mutations executed.")
            print(f"Odoo Operation Sync Status: {sync_res.get('status')} ({sync_res.get('model', sync_res.get('reason'))})")
            sync_graph_audit(ctx, client, "Onboarding Plan")
            if plan.validation_errors:
                print("Plan is not executable until validation errors are resolved.")
                print("==================================================================")
                return 2
            print("Approve the Odoo operation to execute the stored plan via the operation worker.")
            print("==================================================================")
            return 0

        print("\nExecuting Planned Onboarding Mutations...")
        res = service.execute_onboarding(plan, approved=True)

        print(f"Execution Stage:        {res.stage.upper()}")
        print(f"Graph Object ID:        {sanitize(res.graph_id)}")
        print(f"Read-Back Verified:     {res.verification_passed}")
        print("Verified State Details:")
        for k, v in res.verified_properties.items():
            print(f"  - {k}: {v}")

        print("\n--- ODOO ONLINE PERSISTENCE ---")
        sync_res = service.sync_to_odoo(ctx.odoo, res)
        print(f"Odoo Online Sync Status: {sync_res.get('status')}")
        sync_graph_audit(ctx, client, "Onboarding Execute")
        print("==================================================================")
        return 0 if res.verification_passed else 2

    except Exception as exc:
        print(f"Onboarding Failed: {sanitize(exc)}", file=sys.stderr)
        return 2


def run_remediation(ctx: ConnectorContext, action: str, upn: str, group_id: str, sku_id: str, execute: bool) -> int:
    from .clients.ms_graph_client import MSGraphClient
    from .clients.tenant_context import TenantContext
    from .clients.token_provider import MSALTokenProvider
    from .remediation import RemediationService

    if not action or not upn:
        print("Error: --remediate requires --action <action_name> and --upn <user_upn>", file=sys.stderr)
        return 2

    print("==================================================================")
    print(f"HELPDESK REMEDIATION ACTION: '{action.upper()}' ({'EXECUTE' if execute else 'DRY RUN'})")
    print("==================================================================")
    try:
        tenant_context = TenantContext.from_settings(ctx.settings.m365)
        token_provider = MSALTokenProvider(settings=ctx.settings.m365)
        client = MSGraphClient(tenant_context=tenant_context, token_provider=token_provider)
        service = RemediationService(graph_client=client)

        res = service.execute_remediation(
            action=action,
            upn_or_id=upn,
            target_group_id=group_id if group_id else None,
            target_sku_id=sku_id if sku_id else None,
            approved=execute,
            dry_run=not execute,
        )

        print(f"Target User UPN:        {sanitize(res.upn)}")
        print(f"Action Status:          {res.status.upper()}")
        print(f"Approved:               {res.approved}")
        print(f"Read-Back Verified:     {res.verification_passed}")
        print(f"Action Details:         {res.details}")

        print("\n--- ODOO ONLINE PERSISTENCE ---")
        sync_res = service.sync_to_odoo(ctx.odoo, res)
        print(f"Odoo Online Sync Status: {sync_res.get('status')}")
        sync_graph_audit(ctx, client, f"Remediation {action}")
        print("==================================================================")
        return 0 if res.verification_passed else 2

    except Exception as exc:
        print(f"Remediation Action Failed: {sanitize(exc)}", file=sys.stderr)
        return 2


def run_offboarding(ctx: ConnectorContext, upn: str, execute: bool) -> int:
    from .clients.ms_graph_client import MSGraphClient
    from .clients.tenant_context import TenantContext
    from .clients.token_provider import MSALTokenProvider
    from .offboarding import OffboardingService

    if not upn:
        print("Error: --offboard requires --upn <user_upn>", file=sys.stderr)
        return 2

    print("==================================================================")
    print(f"SYNTHETIC EMPLOYEE OFFBOARDING ({'EXECUTE' if execute else 'DRY RUN'})")
    print("==================================================================")
    try:
        tenant_context = TenantContext.from_settings(ctx.settings.m365)
        token_provider = MSALTokenProvider(settings=ctx.settings.m365)
        client = MSGraphClient(tenant_context=tenant_context, token_provider=token_provider)
        service = OffboardingService(graph_client=client)

        res = service.execute_offboarding(upn_or_id=upn, dry_run=not execute, approved=execute)

        print(f"Target UPN:             {sanitize(res.upn)}")
        print(f"Graph Object ID:        {sanitize(res.graph_id)}")
        print(f"Sessions Revoked:       {res.sessions_revoked}")
        print(f"Sign-in Disabled:       {res.signin_disabled}")
        print(f"Removed Group Count:    {len(res.removed_group_ids)}")
        print(f"Removed SKU Count:      {len(res.removed_sku_ids)}")
        print(f"Read-Back Verified:     {res.verification_passed}")
        print(f"Offboarding Status:     {res.status.upper()}")

        print("\n--- ODOO ONLINE PERSISTENCE ---")
        sync_res = service.sync_to_odoo(ctx.odoo, res)
        print(f"Odoo Online Sync Status: {sync_res.get('status')}")
        sync_graph_audit(ctx, client, "Offboarding")
        print("==================================================================")
        return 0 if res.verification_passed else 2

    except Exception as exc:
        print(f"Offboarding Failed: {sanitize(exc)}", file=sys.stderr)
        return 2


def run_test_graph(settings) -> int:
    from .clients.ms_graph_client import MSGraphClient
    from .clients.tenant_context import TenantContext
    from .clients.token_provider import MSALTokenProvider

    print("Executing Microsoft Graph API Connection Test...")
    try:
        tenant_context = TenantContext.from_settings(settings.m365)
        token_provider = MSALTokenProvider(settings=settings.m365)
        client = MSGraphClient(tenant_context=tenant_context, token_provider=token_provider)

        print("Acquiring access token via OAuth 2.0 Client Credentials flow...")
        _ = client.get_access_token()
        print("Token acquisition succeeded! (Access token is sanitized and redacted from logs)")

        print("Executing GET https://graph.microsoft.com/v1.0/users?$top=5 ...")
        res_data = client.get_users(top=5)
        users = res_data.get("value", [])

        print("HTTP 200 OK")
        print(f"Retrieved {len(users)} user record(s) from Microsoft 365 Graph API:")
        for idx, u in enumerate(users, start=1):
            uid = sanitize(u.get("id", "N/A"))
            display_name = sanitize(u.get("displayName", "N/A"))
            email = sanitize(u.get("mail") or u.get("userPrincipalName", "N/A"))
            job_title = sanitize(u.get("jobTitle") or "N/A")
            print(f"  User #{idx}: ID={uid} | Name='{display_name}' | Email='{email}' | Title='{job_title}'")
        return 0
    except IntegrationError as exc:
        print(f"Microsoft Graph Test Failed: {sanitize(exc)}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Unexpected Microsoft Graph Error: {sanitize(exc)}", file=sys.stderr)
        return 3


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

    if args.test_graph:
        return run_test_graph(settings)

    if args.readiness:
        return run_readiness(ctx)

    if args.user_360:
        return run_user_360(ctx, upn=args.upn)

    if args.onboard:
        return run_onboarding(ctx, execute=args.execute)

    if args.remediate:
        return run_remediation(ctx, action=args.action, upn=args.upn, group_id=args.target_group_id, sku_id=args.target_sku_id, execute=args.execute)

    if args.offboard:
        return run_offboarding(ctx, upn=args.upn, execute=args.execute)

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

    if args.drain_requests or args.serve_requests:
        worker = SyncRequestWorker(
            ctx.odoo,
            runner=lambda name: CONNECTORS[name](ctx).run(write_log=not args.no_sync_log),
        )
        if args.serve_requests:
            try:
                worker.serve_forever(poll_seconds=args.poll_seconds)
            except KeyboardInterrupt:
                print("Worker stopped.", file=sys.stderr)
            return 0
        try:
            reports = worker.drain()
        except IntegrationError as exc:
            print(f"Could not read the request queue: {sanitize(exc)}", file=sys.stderr)
            return 3
        if args.json:
            print(json.dumps(reports, indent=2, default=str))
        elif not reports:
            print("No sync was requested; nothing to do.")
        else:
            for report in reports:
                print(f"  {report.get('provider')}: {report.get('status')} - {report.get('summary', report)}")
        statuses = {r.get("status") for r in reports}
        if STATUS_FAILED in statuses:
            return 2
        return 1 if STATUS_PARTIAL in statuses else 0

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
