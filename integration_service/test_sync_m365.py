# -*- coding: utf-8 -*-
"""Test syncing readiness, user 360, and operation data into new x_m365_* models."""
import logging
from integration_service.config import get_settings
from integration_service.connectors.base import build_context
from integration_service.clients.tenant_context import TenantContext
from integration_service.clients.token_provider import MSALTokenProvider
from integration_service.clients.ms_graph_client import MSGraphClient
from integration_service.tenant_readiness import TenantReadinessService
from integration_service.user_360 import User360Service, compare_snapshots
from integration_service.onboarding import OnboardingService

logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger("test_sync")

def main():
    settings = get_settings()
    ctx = build_context(settings=settings)
    
    tenant_context = TenantContext.from_settings(settings.m365)
    token_provider = MSALTokenProvider(settings=settings.m365)
    client = MSGraphClient(tenant_context=tenant_context, token_provider=token_provider)

    # 1. Readiness Sync -> x_m365_tenant
    LOGGER.info("Running Tenant Readiness Service...")
    readiness_svc = TenantReadinessService(graph_client=client)
    report = readiness_svc.run_readiness_check()
    sync_t = readiness_svc.sync_to_odoo(ctx.odoo, report)
    LOGGER.info("Readiness Sync Result: %s", sync_t)

    # 2. User 360 Sync -> x_m365_user_snapshot
    LOGGER.info("Running User 360 Service for Alan Steiner...")
    user360_svc = User360Service(graph_client=client)
    snap = user360_svc.get_user_snapshot("alans@CRMbc388484.OnMicrosoft.com")
    sync_u = user360_svc.sync_to_odoo(ctx.odoo, snap)
    LOGGER.info("User 360 Sync Result: %s", sync_u)

    # 3. Onboarding Plan -> x_m365_operation
    LOGGER.info("Running Onboarding Plan for LAB-User-TestWorker-Auto01...")
    onboard_svc = OnboardingService(graph_client=client)
    plan = onboard_svc.plan_onboarding("TestWorker", "Auto01")
    onboard_res = onboard_svc.execute_onboarding(plan) if False else None
    from integration_service.onboarding import OnboardingResult
    plan_result = OnboardingResult(
        upn=plan.upn,
        graph_id="",
        stage="planned",
        plan=plan,
        verification_passed=False,
    )
    sync_o = onboard_svc.sync_to_odoo(ctx.odoo, plan_result)
    LOGGER.info("Onboarding Plan Sync Result: %s", sync_o)

    # 4. Audit Logger Sync -> x_m365_graph_audit_log
    LOGGER.info("Syncing Audit Logger events...")
    sync_a = client.audit_logger.sync_to_odoo(ctx.odoo, operation_label="Test Sync Batch")
    LOGGER.info("Audit Logger Sync Result: %s", sync_a)

    # Verify records in Odoo
    print("\n--- ODOO VERIFICATION ---")
    print("x_m365_tenant records:", ctx.odoo.search_read("x_m365_tenant", [], fields=["id", "x_name", "x_tenant_id", "x_primary_domain", "x_available_licenses"]))
    print("x_m365_user_snapshot records:", ctx.odoo.search_read("x_m365_user_snapshot", [], fields=["id", "x_name", "x_upn", "x_display_name", "x_job_title"]))
    print("x_m365_operation records:", ctx.odoo.search_read("x_m365_operation", [], fields=["id", "x_name", "x_operation_type", "x_target_upn", "x_state"]))
    print("x_m365_graph_audit_log count:", ctx.odoo.search_count("x_m365_graph_audit_log", []))

if __name__ == "__main__":
    main()
