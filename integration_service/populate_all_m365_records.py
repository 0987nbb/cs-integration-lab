# -*- coding: utf-8 -*-
"""Populate rich live records into all 5 M365 models on Odoo Online."""
import logging
from integration_service.config import get_settings
from integration_service.connectors.base import build_context
from integration_service.clients.tenant_context import TenantContext
from integration_service.clients.token_provider import MSALTokenProvider
from integration_service.clients.ms_graph_client import MSGraphClient
from integration_service.tenant_readiness import TenantReadinessService
from integration_service.user_360 import User360Service, compare_snapshots
from integration_service.onboarding import OnboardingService, OnboardingResult
from integration_service.remediation import RemediationService
from integration_service.offboarding import OffboardingService

logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger("populate_all")

def main():
    settings = get_settings()
    ctx = build_context(settings=settings)
    
    tenant_context = TenantContext.from_settings(settings.m365)
    token_provider = MSALTokenProvider(settings=settings.m365)
    client = MSGraphClient(tenant_context=tenant_context, token_provider=token_provider)

    # 1. Readiness Sync -> x_m365_tenant
    LOGGER.info("1. Syncing Tenant Readiness...")
    readiness_svc = TenantReadinessService(graph_client=client)
    report = readiness_svc.run_readiness_check()
    readiness_svc.sync_to_odoo(ctx.odoo, report)

    # 2. User 360 Snapshots -> x_m365_user_snapshot (2 snapshots for diff)
    LOGGER.info("2. Syncing User 360 Snapshots...")
    user360_svc = User360Service(graph_client=client)
    snap1_obj = user360_svc.get_user_snapshot("alans@CRMbc388484.OnMicrosoft.com")
    sync_u1 = user360_svc.sync_to_odoo(ctx.odoo, snap1_obj)

    # Create a 2nd snapshot for diff
    snap2_dict = snap1_obj.to_dict()
    snap2_dict["snapshot_timestamp"] = "2026-08-14 15:45:00"
    snap2_dict["job_title"] = "Senior VP Corporate Marketing"
    snap2_dict["account_enabled"] = True
    
    # 3. Snapshot Comparison -> x_m365_snapshot_diff
    LOGGER.info("3. Running Snapshot Comparison & Syncing to x_m365_snapshot_diff...")
    diff_res = compare_snapshots(snap1_obj.to_dict(), snap2_dict, odoo_client=ctx.odoo)
    LOGGER.info("Diff result created: %s", diff_res.get("odoo_record_id"))

    # 4. Operations -> x_m365_operation
    LOGGER.info("4. Creating Operations...")
    onboard_svc = OnboardingService(graph_client=client)
    plan = onboard_svc.plan_onboarding("TestWorker", "Auto01")
    plan_result = OnboardingResult(
        upn=plan.upn,
        graph_id="",
        stage="planned",
        plan=plan,
        verification_passed=False,
    )
    onboard_svc.sync_to_odoo(ctx.odoo, plan_result)

    # Create an awaiting_approval operation for testing live execution
    op_vals = {
        "x_name": "M365-REMEDIATE-BLOCK_SIGNIN-alans@CRMbc388484.OnMicrosoft.com",
        "x_operation_type": "remediation",
        "x_remediation_action": "block_signin",
        "x_target_upn": "alans@CRMbc388484.OnMicrosoft.com",
        "x_state": "awaiting_approval",
        "x_planned_mutations": "[]",
        "x_execution_result": "Awaiting approval by manager",
    }
    existing_op = ctx.odoo.search_read("x_m365_operation", [["x_name", "=", op_vals["x_name"]]], fields=["id"])
    if not existing_op:
        ctx.odoo.create_one("x_m365_operation", op_vals)

    # 5. Audit Logs -> x_m365_graph_audit_log
    LOGGER.info("5. Syncing Audit Logs...")
    client.audit_logger.sync_to_odoo(ctx.odoo, operation_label="Verification Run")

    print("\n--- ALL M365 POPULATION COMPLETE ---")

if __name__ == "__main__":
    main()
