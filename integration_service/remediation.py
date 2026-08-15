# -*- coding: utf-8 -*-
"""Helpdesk Remediation Actions Service.

Implements 8 individually callable remediation actions for synthetic test users:
1. block_signin
2. unblock_signin
3. revoke_sessions
4. reset_password
5. add_group
6. remove_group
7. assign_license
8. remove_license

Every state-changing action supports:
- Dry Run
- Explicit Approval
- Pre-action state capture
- Execution
- Post-action Graph verification
- Audit logging & target validation (only approved `LAB-*` groups and tenant SKUs allowed).
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .clients.ms_graph_client import MSGraphClient
from .clients.tenant_context import TenantContext
from .clients.token_provider import TokenProvider
from .errors import AuthorizationError, ClientError, GraphProtocolError, HttpError, InvalidPayloadError
from .sanitize import sanitize
from .tenant_readiness import TenantReadinessService

LOGGER = logging.getLogger("integration_service.ms_graph.remediation")

ALLOWED_REMEDIATION_ACTIONS = {
    "block_signin",
    "unblock_signin",
    "revoke_sessions",
    "reset_password",
    "add_group",
    "remove_group",
    "assign_license",
    "remove_license",
}


@dataclass
class RemediationResult:
    """Execution and verification result of a remediation action."""

    action: str
    upn: str
    graph_id: str
    approved: bool
    dry_run: bool
    pre_action_state: Dict[str, Any] = field(default_factory=dict)
    post_action_state: Dict[str, Any] = field(default_factory=dict)
    verification_passed: bool = False
    execution_timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    status: str = "success"  # "success", "dry_run", "failed", "unapproved"
    details: str = ""
    error_details: Optional[str] = None
    verification_method: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class RemediationService:
    """Service that executes controlled helpdesk remediation actions."""

    def __init__(
        self,
        graph_client: Optional[MSGraphClient] = None,
        tenant_context: Optional[TenantContext] = None,
        token_provider: Optional[TokenProvider] = None,
    ) -> None:
        if graph_client is not None:
            self.client = graph_client
        else:
            self.client = MSGraphClient(
                tenant_context=tenant_context,
                token_provider=token_provider,
            )

    def _capture_user_state(self, upn_or_id: str) -> Dict[str, Any]:
        """Capture current Graph state for a user before or after an action."""
        try:
            res = self.client.get(
                "users/"
                f"{upn_or_id}"
                "?$select=id,userPrincipalName,displayName,accountEnabled,assignedLicenses,"
                "signInSessionsValidFromDateTime",
                operation_name="REMEDIATION_CAPTURE_STATE",
            )
            u_data = res.data if isinstance(res.data, dict) else {}
            
            groups: List[str] = []
            for g in self.client.paginate(f"users/{upn_or_id}/memberOf", operation_name="REMEDIATION_CAPTURE_GROUPS"):
                if g.get("id"):
                    groups.append(g["id"])

            return {
                "id": u_data.get("id"),
                "upn": u_data.get("userPrincipalName"),
                "displayName": u_data.get("displayName"),
                "accountEnabled": u_data.get("accountEnabled"),
                "assignedLicenses": u_data.get("assignedLicenses", []),
                "signInSessionsValidFromDateTime": u_data.get("signInSessionsValidFromDateTime"),
                "group_ids": groups,
            }
        except Exception as exc:
            LOGGER.warning("Could not capture pre/post state for %s: %s", sanitize(upn_or_id), sanitize(exc))
            return {"error": sanitize(exc)}

    def execute_remediation(
        self,
        action: str,
        upn_or_id: str,
        target_group_id: Optional[str] = None,
        target_sku_id: Optional[str] = None,
        new_temp_password: Optional[str] = None,
        approved: bool = False,
        dry_run: bool = False,
    ) -> RemediationResult:
        """Execute specified remediation action with dry run, approval, pre-capture, and post-verification."""
        LOGGER.info("Executing remediation action '%s' for %s (approved=%s, dry_run=%s)", action, sanitize(upn_or_id), approved, dry_run)

        if action not in ALLOWED_REMEDIATION_ACTIONS:
            raise InvalidPayloadError(f"Unknown remediation action: {action}")

        if action in {"add_group", "remove_group"} and not target_group_id:
            raise InvalidPayloadError(f"{action} requires target_group_id")

        if action in {"assign_license", "remove_license"} and not target_sku_id:
            raise InvalidPayloadError(f"{action} requires target_sku_id")

        # 1. Target Validation Rule (LAB-* Safety Boundary)
        if (approved or not dry_run) and not (upn_or_id.startswith("LAB-") or "lab-" in upn_or_id.lower()):
            raise InvalidPayloadError(
                f"Safety Violation: Target user '{upn_or_id}' does not match the synthetic LAB-* naming convention. "
                "Graph mutations are strictly restricted to synthetic LAB-* test users."
            )

        readiness_svc = TenantReadinessService(graph_client=self.client)
        target_group_name = None
        if target_group_id:
            lab_groups = readiness_svc.discover_lab_groups(prefix="LAB-")
            valid_groups = {g["graph_id"]: g for g in lab_groups if g.get("graph_id")}
            if target_group_id not in valid_groups:
                valid_gids = sorted(valid_groups)
                raise InvalidPayloadError(f"Target group ID {target_group_id} is not in approved LAB-* test groups: {valid_gids}")
            target_group_name = valid_groups[target_group_id].get("name")

        target_sku = None
        if target_sku_id:
            sku_res = readiness_svc.discover_skus()
            valid_skus = {s["sku_id"]: s for s in sku_res.get("skus", []) if s.get("sku_id")}
            if target_sku_id not in valid_skus:
                valid_sku_ids = sorted(valid_skus)
                raise InvalidPayloadError(f"Target SKU ID {target_sku_id} is not in discovered tenant SKUs: {valid_sku_ids}")
            target_sku = valid_skus[target_sku_id]

        # 2. Pre-action State Capture
        pre_state = self._capture_user_state(upn_or_id)
        graph_id = pre_state.get("id") or upn_or_id
        if pre_state.get("error") or not pre_state.get("id"):
            err = pre_state.get("error") or "Target user was not found/readable through Graph before mutation."
            return RemediationResult(
                action=action,
                upn=upn_or_id,
                graph_id=graph_id,
                approved=approved,
                dry_run=dry_run,
                pre_action_state=pre_state,
                post_action_state=pre_state,
                verification_passed=False,
                status="failed",
                details=f"Pre-action state capture failed: {err}",
                error_details=str(err),
            )

        assigned_sku_ids = {lic.get("skuId") for lic in pre_state.get("assignedLicenses", []) if isinstance(lic, dict)}
        if action == "assign_license" and target_sku_id in assigned_sku_ids:
            license_already_assigned = True
        else:
            license_already_assigned = False
        if (
            action == "assign_license"
            and not license_already_assigned
            and int((target_sku or {}).get("available_units") or 0) <= 0
        ):
            raise InvalidPayloadError(
                f"Target SKU ID {target_sku_id} has no available license capacity; "
                f"available_units={(target_sku or {}).get('available_units')}"
            )

        if dry_run or not approved:
            target_desc = ""
            if target_group_id:
                target_desc = f" Target group: {target_group_name or target_group_id}."
            elif target_sku_id:
                target_desc = f" Target SKU: {(target_sku or {}).get('sku_part_number') or target_sku_id}."
            return RemediationResult(
                action=action,
                upn=upn_or_id,
                graph_id=graph_id,
                approved=approved,
                dry_run=dry_run,
                pre_action_state=pre_state,
                post_action_state=pre_state,
                verification_passed=True if dry_run else False,
                status="dry_run" if dry_run else "unapproved",
                details=f"[DRY RUN / PLAN] Action '{action}' planned for {upn_or_id}.{target_desc} No M365 mutations executed.",
                verification_method="pre-action Graph read captured; execution intentionally skipped",
            )

        # 3. Execution Phase
        mutation_executed = False
        try:
            if action == "block_signin":
                if pre_state.get("accountEnabled") is not False:
                    self.client.patch(f"users/{graph_id}", json_data={"accountEnabled": False}, operation_name="REM_BLOCK_SIGNIN")
                    mutation_executed = True

            elif action == "unblock_signin":
                if pre_state.get("accountEnabled") is not True:
                    self.client.patch(f"users/{graph_id}", json_data={"accountEnabled": True}, operation_name="REM_UNBLOCK_SIGNIN")
                    mutation_executed = True

            elif action == "revoke_sessions":
                self.client.post(f"users/{graph_id}/revokeSignInSessions", operation_name="REM_REVOKE_SESSIONS")
                mutation_executed = True

            elif action == "reset_password":
                pass_val = new_temp_password or "LAB#PassReset2026!"
                payload = {"passwordProfile": {"forceChangePasswordNextSignIn": True, "password": pass_val}}
                self.client.patch(f"users/{graph_id}", json_data=payload, operation_name="REM_RESET_PASSWORD")
                mutation_executed = True

            elif action == "add_group":
                # Check if already member
                if target_group_id not in pre_state.get("group_ids", []):
                    ref_payload = {"@odata.id": f"{self.client.tenant_context.graph_base_url}/directoryObjects/{graph_id}"}
                    self.client.post(f"groups/{target_group_id}/members/$ref", json_data=ref_payload, operation_name="REM_ADD_GROUP")
                    mutation_executed = True

            elif action == "remove_group":
                # Check if member
                if target_group_id in pre_state.get("group_ids", []):
                    self.client.delete(f"groups/{target_group_id}/members/{graph_id}/$ref", operation_name="REM_REMOVE_GROUP")
                    mutation_executed = True

            elif action == "assign_license":
                if not license_already_assigned:
                    lic_payload = {"addLicenses": [{"skuId": target_sku_id}], "removeLicenses": []}
                    self.client.post(f"users/{graph_id}/assignLicense", json_data=lic_payload, operation_name="REM_ASSIGN_LICENSE")
                    mutation_executed = True

            elif action == "remove_license":
                if target_sku_id in assigned_sku_ids:
                    lic_payload = {"addLicenses": [], "removeLicenses": [target_sku_id]}
                    self.client.post(f"users/{graph_id}/assignLicense", json_data=lic_payload, operation_name="REM_REMOVE_LICENSE")
                    mutation_executed = True

        except Exception as exc:
            sanitized_err = sanitize(exc)
            LOGGER.error("Remediation action '%s' failed for %s: %s", action, sanitize(upn_or_id), sanitized_err)
            return RemediationResult(
                action=action,
                upn=upn_or_id,
                graph_id=graph_id,
                approved=approved,
                dry_run=dry_run,
                pre_action_state=pre_state,
                post_action_state=pre_state,
                verification_passed=False,
                status="failed",
                details=f"Remediation failed: {sanitized_err}",
                error_details=str(sanitized_err),
            )

        # 4. Post-action Graph Verification
        post_state = self._capture_user_state(graph_id)
        verification_passed = False
        verification_method = "post-action Graph read-back"

        if action == "block_signin":
            verification_passed = post_state.get("accountEnabled") is False
        elif action == "unblock_signin":
            verification_passed = post_state.get("accountEnabled") is True
        elif action == "add_group":
            verification_passed = target_group_id in post_state.get("group_ids", [])
        elif action == "remove_group":
            verification_passed = target_group_id not in post_state.get("group_ids", [])
        elif action == "assign_license":
            verification_passed = any(s.get("skuId") == target_sku_id for s in post_state.get("assignedLicenses", []))
        elif action == "remove_license":
            verification_passed = not any(s.get("skuId") == target_sku_id for s in post_state.get("assignedLicenses", []))
        elif action == "revoke_sessions":
            before = pre_state.get("signInSessionsValidFromDateTime")
            after = post_state.get("signInSessionsValidFromDateTime")
            if before and after:
                verification_passed = after >= before
                verification_method = "post-action signInSessionsValidFromDateTime read-back"
            else:
                verification_passed = bool(post_state.get("id")) and not post_state.get("error")
                verification_method = "post-action user read-back; session timestamp unavailable in tenant response"
        elif action == "reset_password":
            verification_passed = bool(post_state.get("id")) and not post_state.get("error")
            verification_method = "post-action user read-back; passwordProfile is write-only"
        else:
            verification_passed = False

        action_detail = "no mutation needed; desired state already existed" if not mutation_executed else "mutation executed"
        return RemediationResult(
            action=action,
            upn=upn_or_id,
            graph_id=graph_id,
            approved=approved,
            dry_run=dry_run,
            pre_action_state=pre_state,
            post_action_state=post_state,
            verification_passed=verification_passed,
            status="success" if verification_passed else "failed",
            details=f"Action '{action}' {action_detail} and verified via Graph read-back.",
            error_details=None if verification_passed else "Post-action verification failed.",
            verification_method=verification_method,
        )

    def sync_to_odoo(self, odoo_client: Any, result: RemediationResult) -> Dict[str, Any]:
        """Persist remediation action result to Odoo control plane."""
        if not odoo_client:
            return {"status": "skipped", "reason": "No Odoo client provided"}

        import json

        summary_msg = (
            f"Remediation Action '{result.action.upper()}' ({result.status.upper()})\n"
            f"Target: {result.upn} | Verified: {result.verification_passed}\n"
            f"Details: {result.details}"
        )
        notes_payload = result.to_dict()
        notes_json = json.dumps(notes_payload, indent=2, ensure_ascii=False)

        try:
            def supported_vals(model: str, vals: Dict[str, Any]) -> Dict[str, Any]:
                try:
                    fields_meta = odoo_client.fields_get(model)
                    return {k: v for k, v in vals.items() if k in fields_meta}
                except Exception:
                    return vals

            if odoo_client.model_exists("cs.m365.operation"):
                # -- Primary path: cs.m365.operation (self-hosted Odoo with addon) --
                ts_str = result.execution_timestamp.replace("T", " ")[:19] if result.execution_timestamp else False
                state = "verified" if result.verification_passed else "failed"
                op_name = f"M365-REMEDIATE-{result.action.upper()}-{result.upn}"
                op_vals = {
                    "operation_type": "remediation",
                    "target_upn": result.upn,
                    "created_at": ts_str,
                    "started_at": ts_str,
                    "finished_at": ts_str,
                    "state": state,
                    "result": str(result.details or ""),
                    "error_details": sanitize(result.error_details) if result.error_details else False,
                    "last_successful_step": result.action,
                    "notes": notes_json,
                }
                op_vals = supported_vals("cs.m365.operation", op_vals)
                existing = odoo_client.search_read(
                    "cs.m365.operation",
                    [
                        ["target_upn", "=", result.upn],
                        ["operation_type", "=", "remediation"],
                        ["last_successful_step", "=", result.action],
                    ],
                    fields=["id"],
                    limit=1,
                )
                if existing:
                    odoo_client.write("cs.m365.operation", [existing[0]["id"]], op_vals)
                    rec_id = existing[0]["id"]
                else:
                    if "name" in supported_vals("cs.m365.operation", {"name": op_name}):
                        op_vals["name"] = op_name
                    rec_id = odoo_client.create_one("cs.m365.operation", op_vals)

                LOGGER.info(
                    "Persisted Remediation Result '%s' for %s to cs.m365.operation (id=%s)",
                    result.action, result.upn, rec_id,
                )
                return {"status": "success", "model": "cs.m365.operation", "action": result.action, "upn": result.upn, "record_id": rec_id}

            elif odoo_client.model_exists("x_m365_operation"):
                # -- Dedicated Studio model: x_m365_operation (Odoo Online) --
                ts_str = result.execution_timestamp.replace("T", " ")[:19] if result.execution_timestamp else False
                state = "verified" if result.verification_passed else ("planned" if result.dry_run else "failed")
                op_name = f"M365-REMEDIATE-{result.action.upper()}-{result.upn}"
                op_vals = {
                    "x_name": op_name,
                    "x_operation_type": "remediation",
                    "x_remediation_action": result.action,
                    "x_target_upn": result.upn,
                    "x_target_graph_id": result.graph_id or "",
                    "x_state": state,
                    "x_execution_result": str(result.details or ""),
                    "x_verification_passed": result.verification_passed,
                    "x_created_at": ts_str,
                    "x_finished_at": ts_str,
                    "x_notes": notes_json,
                }
                op_vals = supported_vals("x_m365_operation", op_vals)
                existing = odoo_client.search_read(
                    "x_m365_operation",
                    [["x_target_upn", "=", result.upn], ["x_operation_type", "=", "remediation"], ["x_remediation_action", "=", result.action]],
                    fields=["id"],
                    limit=1,
                )
                if existing:
                    odoo_client.write("x_m365_operation", [existing[0]["id"]], op_vals)
                    rec_id = existing[0]["id"]
                else:
                    rec_id = odoo_client.create_one("x_m365_operation", op_vals)

                LOGGER.info("Persisted Remediation Result '%s' for %s to x_m365_operation (id=%s)", result.action, result.upn, rec_id)
                return {"status": "success", "model": "x_m365_operation", "action": result.action, "upn": result.upn, "record_id": rec_id}

            elif odoo_client.model_exists("x_integration_config"):
                # -- Fallback: x_integration_config (Odoo Online without addon) --
                config_name = f"Microsoft 365 Remediation: {result.action} ({result.upn})"
                last_check_str = result.execution_timestamp.replace("T", " ")[:19] if result.execution_timestamp else False
                vals = {
                    "x_name": config_name,
                    "x_provider": "m365_graph",
                    "x_sync_state": "done" if result.verification_passed else "failed",
                    "x_last_sync_at": last_check_str,
                    "x_notes": notes_json,
                    "x_sync_message": summary_msg,
                    "x_active": True,
                }
                vals = supported_vals("x_integration_config", vals)
                existing = odoo_client.search_read("x_integration_config", [["x_name", "=", config_name]], fields=["id"], limit=1)
                if existing:
                    odoo_client.write("x_integration_config", [existing[0]["id"]], vals)
                    rec_id = existing[0]["id"]
                else:
                    rec_id = odoo_client.create_one("x_integration_config", vals)

                LOGGER.info(
                    "Persisted Remediation Result '%s' for %s to Odoo Online (x_integration_config id=%s)",
                    result.action, result.upn, rec_id,
                )
                return {"status": "success", "model": "x_integration_config", "action": result.action, "upn": result.upn, "record_id": rec_id}
            else:
                return {"status": "skipped", "reason": "No compatible Odoo model accessible"}

        except Exception as exc:
            sanitized_err = sanitize(exc)
            LOGGER.warning("Could not persist Remediation Result to Odoo Online: %s", sanitized_err)
            return {"status": "fallback", "reason": sanitized_err}


__all__ = ["RemediationService", "RemediationResult"]
