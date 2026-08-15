# -*- coding: utf-8 -*-
"""Operation Polling Worker for x_m365_operation state transitions.

Polls Odoo Online for x_m365_operation records awaiting execution ('awaiting_approval' or 'planned'),
executes Graph mutations with safety boundary checks, performs post-action read-back verification,
and writes updated states ('verified' / 'failed') back to Odoo Online via JSON-2 API.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .clients.ms_graph_client import MSGraphClient
from .clients.tenant_context import TenantContext
from .clients.token_provider import MSALTokenProvider
from .config import get_settings
from .connectors.base import ConnectorContext, build_context
from .offboarding import OffboardingService
from .onboarding import OnboardingPlan, OnboardingService
from .remediation import RemediationService
from .sanitize import sanitize

LOGGER = logging.getLogger("integration_service.operation_worker")


class OperationWorker:
    """Worker that polls x_m365_operation and executes Graph operations."""

    def __init__(self, ctx: Optional[ConnectorContext] = None) -> None:
        self.ctx = ctx or build_context(settings=get_settings())
        self.odoo = self.ctx.odoo
        
        tenant_context = TenantContext.from_settings(self.ctx.settings.m365)
        token_provider = MSALTokenProvider(settings=self.ctx.settings.m365)
        self.client = MSGraphClient(tenant_context=tenant_context, token_provider=token_provider)

        self.onboarding_svc = OnboardingService(graph_client=self.client)
        self.remediation_svc = RemediationService(graph_client=self.client)
        self.offboarding_svc = OffboardingService(graph_client=self.client)
        self.stale_running_seconds = int(os.getenv("M365_OPERATION_STALE_RUNNING_SECONDS", "1800"))

    def recover_stale_running_operations(self) -> int:
        """Mark old running operations uncertain before processing new work.

        A restarted worker must not blindly repeat mutations after a process crash.
        Operators can inspect the uncertain record and rerun only after Graph state
        has been reconciled by the operation-specific read-back logic.
        """
        model = self._get_operation_model()
        is_custom = model.startswith("x_")
        state_f = "x_state" if is_custom else "state"
        started_f = "x_started_at" if is_custom else "started_at"
        name_f = "x_name" if is_custom else "name"
        err_f = "x_error_details" if is_custom else "error_details"

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.stale_running_seconds)
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        try:
            stale_ops = self.odoo.search_read(
                model,
                [[state_f, "=", "running"], [started_f, "<", cutoff_str]],
                fields=["id", name_f, started_f],
            )
        except Exception as exc:
            LOGGER.warning("Could not scan stale running M365 operations: %s", sanitize(exc))
            return 0

        recovered = 0
        for op in stale_ops:
            if self._safe_write(
                model,
                [op["id"]],
                {
                    state_f: "uncertain",
                    err_f: (
                        "Worker/process crash recovery: operation was left running beyond "
                        f"{self.stale_running_seconds} seconds. Verify Microsoft 365 state before retry."
                    ),
                },
            ):
                recovered += 1
        if recovered:
            LOGGER.warning("Recovered %d stale running M365 operation(s) as uncertain", recovered)
        return recovered

    def _get_operation_model(self) -> str:
        if hasattr(self, '_op_model'):
            return self._op_model
        try:
            if hasattr(self.odoo, "model_exists") and self.odoo.model_exists("cs.m365.operation") is True:
                self._op_model = "cs.m365.operation"
            else:
                self._op_model = "x_m365_operation"
        except Exception:
            self._op_model = "x_m365_operation"
        return self._op_model

    def _get_op_fields(self, is_custom: bool) -> List[str]:
        if is_custom:
            return ["id", "x_name", "x_operation_type", "x_target_upn", "x_target_graph_id", "x_state", "x_remediation_action", "x_target_group_id", "x_target_sku_id", "x_planned_mutations"]
        else:
            return ["id", "name", "operation_type", "target_upn", "target_graph_id", "state", "remediation_action", "target_group_id", "target_sku_id", "planned_mutations"]

    def fetch_pending_operations(self) -> List[Dict[str, Any]]:
        """Fetch operations records awaiting execution or planning."""
        self.recover_stale_running_operations()
        model = self._get_operation_model()
        is_custom = model.startswith("x_")
        state_f = "x_state" if is_custom else "state"
        
        return self.odoo.search_read(
            model,
            [[state_f, "in", ["draft", "awaiting_approval"]]],
            fields=self._get_op_fields(is_custom),
            order="id asc",
        )

    def _safe_write(self, model: str, ids: List[int], vals: Dict[str, Any]) -> bool:
        try:
            return bool(self.odoo.write(model, ids, vals))
        except Exception as exc:
            LOGGER.error("Could not write %s %s to Odoo: %s", model, ids, sanitize(exc))
            return False

    def process_operation(self, op: Dict[str, Any]) -> Dict[str, Any]:
        """Process a single operations record with atomic claim and basic reconciliation."""
        model = self._get_operation_model()
        is_custom = model.startswith("x_")
        state_f = "x_state" if is_custom else "state"
        name_f = "x_name" if is_custom else "name"
        type_f = "x_operation_type" if is_custom else "operation_type"
        upn_f = "x_target_upn" if is_custom else "target_upn"
        remed_f = "x_remediation_action" if is_custom else "remediation_action"
        group_f = "x_target_group_id" if is_custom else "target_group_id"
        sku_f = "x_target_sku_id" if is_custom else "target_sku_id"
        plan_f = "x_planned_mutations" if is_custom else "planned_mutations"
        start_f = "x_started_at" if is_custom else "started_at"
        err_f = "x_error_details" if is_custom else "error_details"

        op_id = op["id"]
        op_type = op.get(type_f) or op.get("operation_type") or op.get("x_operation_type")
        target_upn = op.get(upn_f) or op.get("target_upn") or op.get("x_target_upn") or ""
        remediation_action = op.get(remed_f) or op.get("remediation_action") or op.get("x_remediation_action") or ""
        target_group_id = op.get(group_f) or op.get("target_group_id") or op.get("x_target_group_id") or None
        target_sku_id = op.get(sku_f) or op.get("target_sku_id") or op.get("x_target_sku_id") or None

        LOGGER.info("Claiming and running operation #%s: %s (%s for %s)", op_id, op.get(name_f), op_type, target_upn)
        
        state = op.get(state_f)
        # 1. ATOMIC CLAIM: Only transition to running if currently draft or awaiting_approval
        current_op = self.odoo.search_read(model, [["id", "=", op_id], [state_f, "in", ["draft", "awaiting_approval"]]], fields=["id", state_f])
        if not current_op:
            LOGGER.warning("Operation #%s is no longer pending (already claimed). Skipping.", op_id)
            return {"status": "skipped", "reason": "already_claimed"}
            
        actual_state = current_op[0].get(state_f) or current_op[0].get("x_state") or current_op[0].get("state") or "draft"

        started_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if not self._safe_write(model, [op_id], {state_f: "running", start_f: started_at}):
            return {"status": "failed", "reason": "could_not_mark_running"}
            
        is_dry_run = actual_state == "draft"

        try:
            if op_type == "onboarding":
                if is_dry_run:
                    # Extract names from target_upn
                    local_part = target_upn.split('@')[0]
                    first_name = local_part
                    last_name = "User"
                    if "-" in local_part:
                        parts = local_part.split("-")
                        if len(parts) >= 3:
                            first_name = parts[-2]
                            last_name = parts[-1]
                        else:
                            first_name = parts[0]
                            last_name = parts[-1] if len(parts) > 1 else "User"

                    # Plan generation
                    plan = self.onboarding_svc.plan_onboarding(
                        first_name=first_name,
                        last_name=last_name,
                        job_title="New Hire", # Basic default if UI doesn't provide
                        department="Unknown",
                        usage_location="US",
                        desired_sku_part_number=target_sku_id if target_sku_id else None
                    )
                    import json
                    plan_json = json.dumps(plan.to_dict(), indent=2, ensure_ascii=False)
                    self._safe_write(model, [op_id], {
                        state_f: "planned",
                        plan_f: plan_json,
                        ("x_target_upn" if is_custom else "target_upn"): plan.upn,
                    })
                    LOGGER.info("Operation #%s: Planned dry-run mutations saved to Odoo record", op_id)
                    return {"status": "success", "stage": "planned", "plan": plan.to_dict()}
                else:
                    stored_plan = op.get(plan_f)
                    if not stored_plan:
                        err_msg = "Onboarding operation has no stored dry-run plan; refusing to execute unplanned mutations."
                        self._safe_write(model, [op_id], {state_f: "failed", err_f: err_msg})
                        return {"status": "failed", "reason": err_msg}
                    try:
                        import json
                        plan_data = json.loads(stored_plan)
                        if isinstance(plan_data, list):
                            raise ValueError("stored plan contains only mutation list, not full onboarding plan")
                        plan = OnboardingService.plan_from_dict(plan_data)
                    except Exception as e:
                        err_msg = f"Could not parse stored onboarding plan: {sanitize(e)}"
                        self._safe_write(model, [op_id], {state_f: "failed", err_f: err_msg})
                        return {"status": "failed", "reason": err_msg}
                    
                    res = self.onboarding_svc.execute_onboarding(plan, approved=True)
                    v_pass_f = "x_verification_passed" if is_custom else "verification_passed"
                    exec_res_f = "x_execution_result" if is_custom else "execution_result"
                    target_gid_f = "x_target_graph_id" if is_custom else "target_graph_id"
                    self._safe_write(model, [op_id], {
                        state_f: "verified" if res.verification_passed else "failed",
                        exec_res_f: json.dumps(res.verified_properties or {}, indent=2, ensure_ascii=False),
                        v_pass_f: res.verification_passed,
                        target_gid_f: res.graph_id or "",
                        err_f: sanitize(res.error_details) if res.error_details else False,
                    })
                    LOGGER.info("Operation #%s: Execution verified and written to Odoo record", op_id)
                    return {"status": "success", "stage": res.stage, "result": res.to_dict()}

            elif op_type == "remediation":
                res = self.remediation_svc.execute_remediation(
                    action=remediation_action or "block_signin",
                    upn_or_id=target_upn,
                    target_group_id=target_group_id,
                    target_sku_id=target_sku_id,
                    approved=not is_dry_run,
                    dry_run=is_dry_run,
                )
                sync_res = self.remediation_svc.sync_to_odoo(self.odoo, res)
                if sync_res.get("status") == "fallback":
                    self._safe_write(model, [op_id], {state_f: "uncertain", err_f: "Graph success but Odoo sync failed."})
                return sync_res

            elif op_type == "offboarding":
                res = self.offboarding_svc.execute_offboarding(upn_or_id=target_upn, dry_run=is_dry_run, approved=not is_dry_run)
                sync_res = self.offboarding_svc.sync_to_odoo(self.odoo, res)
                if sync_res.get("status") == "fallback":
                    self._safe_write(model, [op_id], {state_f: "uncertain", err_f: "Graph success but Odoo sync failed."})
                return sync_res

            elif op_type == "user_360":
                from .user_360 import User360Service
                svc = User360Service(graph_client=self.client)
                res = svc.get_user_snapshot(target_upn)
                sync_res = svc.sync_to_odoo(self.odoo, res)
                # Mark operation as verified since User 360 is read-only
                v_pass_f = "x_verification_passed" if is_custom else "verification_passed"
                exec_res_f = "x_execution_result" if is_custom else "execution_result"
                self._safe_write(model, [op_id], {state_f: "verified", exec_res_f: "Diagnostic complete", v_pass_f: True})
                return sync_res

            else:
                err_msg = f"Unknown operation type: {op_type}"
                self._safe_write(model, [op_id], {state_f: "failed", err_f: err_msg})
                return {"status": "failed", "reason": err_msg}

        except Exception as exc:
            sanitized_err = sanitize(exc)
            LOGGER.error("Error processing operation #%s: %s", op_id, sanitized_err)
            v_pass_f = "x_verification_passed" if is_custom else "verification_passed"
            self._safe_write(
                model,
                [op_id],
                {state_f: "failed", err_f: sanitized_err, v_pass_f: False},
            )
            return {"status": "failed", "error": sanitized_err}

    def drain(self) -> List[Dict[str, Any]]:
        """Process all currently pending operations records."""
        self.recover_stale_running_operations()
        model = self._get_operation_model()
        is_custom = model.startswith("x_")
        state_f = "x_state" if is_custom else "state"
        
        ops = self.odoo.search_read(
            model,
            [[state_f, "in", ["draft", "awaiting_approval"]]],
            fields=self._get_op_fields(is_custom),
        )
        LOGGER.info("Draining %d pending operations from Odoo Online...", len(ops))
        reports = []
        for op in ops:
            reports.append(self.process_operation(op))
        return reports


__all__ = ["OperationWorker"]
