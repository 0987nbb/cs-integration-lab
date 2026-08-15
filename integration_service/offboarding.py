# -*- coding: utf-8 -*-
"""Synthetic Employee Offboarding Service.

Implements safe, idempotent offboarding for synthetic `LAB-*` test users:
1. Capture current Graph state (identity, sign-in state, groups, licenses).
2. Revoke active sign-in sessions.
3. Disable sign-in (`accountEnabled = False`).
4. Remove removable `LAB-*` group memberships (preserving configured protected groups).
5. Remove assigned removable licenses.
6. Read back final state from Graph and produce verification report.
7. Does NOT delete the user.
8. Idempotent: Re-running offboarding reads current Graph state first and avoids unnecessary mutations.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from .clients.ms_graph_client import MSGraphClient
from .clients.tenant_context import TenantContext
from .clients.token_provider import TokenProvider
from .errors import AuthorizationError, ClientError, GraphProtocolError, HttpError
from .sanitize import sanitize
from .tenant_readiness import TenantReadinessService

LOGGER = logging.getLogger("integration_service.ms_graph.offboarding")


@dataclass
class OffboardingResult:
    """Execution and verification report for synthetic offboarding."""

    upn: str
    graph_id: str
    sessions_revoked: bool
    signin_disabled: bool
    removed_group_ids: List[str] = field(default_factory=list)
    preserved_group_ids: List[str] = field(default_factory=list)
    removed_sku_ids: List[str] = field(default_factory=list)
    preserved_sku_ids: List[str] = field(default_factory=list)
    verification_report: Dict[str, Any] = field(default_factory=dict)
    pre_offboarding_state: Dict[str, Any] = field(default_factory=dict)
    post_offboarding_state: Dict[str, Any] = field(default_factory=dict)
    verification_passed: bool = False
    offboarded_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    status: str = "success"  # "success", "partial", "failed"
    error_details: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class OffboardingService:
    """Service that manages synthetic employee offboarding."""

    def __init__(
        self,
        graph_client: Optional[MSGraphClient] = None,
        tenant_context: Optional[TenantContext] = None,
        token_provider: Optional[TokenProvider] = None,
        protected_group_ids: Optional[Set[str]] = None,
        removable_sku_ids: Optional[Set[str]] = None,
    ) -> None:
        if graph_client is not None:
            self.client = graph_client
        else:
            self.client = MSGraphClient(
                tenant_context=tenant_context,
                token_provider=token_provider,
            )
        self.protected_group_ids = protected_group_ids or set()
        self.removable_sku_ids = removable_sku_ids

    def _capture_state(self, upn_or_id: str) -> Dict[str, Any]:
        """Capture user state from Graph."""
        try:
            res = self.client.get(
                "users/"
                f"{upn_or_id}"
                "?$select=id,userPrincipalName,displayName,accountEnabled,assignedLicenses,"
                "signInSessionsValidFromDateTime",
                operation_name="OFFBOARD_CAPTURE_STATE",
            )
            u_data = res.data if isinstance(res.data, dict) else {}
            
            groups: List[Dict[str, Any]] = []
            for g in self.client.paginate(f"users/{upn_or_id}/memberOf", operation_name="OFFBOARD_CAPTURE_GROUPS"):
                if g.get("id"):
                    groups.append({
                        "id": g["id"],
                        "displayName": g.get("displayName", ""),
                    })

            return {
                "id": u_data.get("id"),
                "upn": u_data.get("userPrincipalName"),
                "displayName": u_data.get("displayName"),
                "accountEnabled": u_data.get("accountEnabled"),
                "assignedLicenses": u_data.get("assignedLicenses", []),
                "signInSessionsValidFromDateTime": u_data.get("signInSessionsValidFromDateTime"),
                "groups": groups,
            }
        except Exception as exc:
            LOGGER.warning("Could not capture offboarding state for %s: %s", sanitize(upn_or_id), sanitize(exc))
            return {"error": sanitize(exc)}

    def execute_offboarding(
        self,
        upn_or_id: str,
        dry_run: bool = False,
        approved: bool = False,
    ) -> OffboardingResult:
        """Execute Synthetic Employee Offboarding."""
        LOGGER.info("Executing synthetic offboarding for %s (dry_run=%s, approved=%s)", sanitize(upn_or_id), dry_run, approved)

        # 0. Safety Boundary Enforcement
        if not (upn_or_id.startswith("LAB-") or "lab-" in upn_or_id.lower()):
            from .errors import InvalidPayloadError
            raise InvalidPayloadError(
                f"Safety Violation: Target user '{upn_or_id}' does not match the synthetic LAB-* naming convention. "
                "Offboarding mutations are strictly restricted to synthetic LAB-* test users."
            )

        # 1. Capture Pre-offboarding State
        pre_state = self._capture_state(upn_or_id)
        graph_id = pre_state.get("id") or upn_or_id
        if not pre_state.get("id"):
            return OffboardingResult(
                upn=upn_or_id,
                graph_id="",
                sessions_revoked=False,
                signin_disabled=False,
                verification_passed=False,
                status="failed",
                error_details=f"User {upn_or_id} not found in Microsoft Graph",
            )

        if dry_run or not approved:
            removable_groups = self._removable_groups(pre_state)
            removable_skus, preserved_skus = self._split_removable_skus(pre_state)
            return OffboardingResult(
                upn=upn_or_id,
                graph_id=graph_id,
                sessions_revoked=False,
                signin_disabled=False,
                pre_offboarding_state=pre_state,
                post_offboarding_state=pre_state,
                verification_passed=True,
                status="dry_run" if dry_run else "unapproved",
                verification_report={
                    "planned_revoke_sessions": bool(removable_groups or removable_skus or pre_state.get("accountEnabled") is not False),
                    "planned_disable_signin": pre_state.get("accountEnabled") is not False,
                    "planned_remove_group_ids": [g["id"] for g in removable_groups],
                    "planned_remove_sku_ids": removable_skus,
                    "preserved_sku_ids": preserved_skus,
                },
                error_details="[DRY RUN / PLAN] Offboarding planned. No Graph mutations executed." if dry_run else "Operation not explicitly approved.",
            )

        removable_groups = self._removable_groups(pre_state)
        preserved_group_ids = self._preserved_group_ids(pre_state)
        removable_sku_ids, preserved_sku_ids = self._split_removable_skus(pre_state)

        offboarding_needed = (
            pre_state.get("accountEnabled") is not False
            or bool(removable_groups)
            or bool(removable_sku_ids)
        )

        # 2. Revoke Sessions. On rerun, skip when final offboarded state already exists.
        sessions_revoked = False
        mutation_errors: List[str] = []
        if offboarding_needed:
            try:
                self.client.post(f"users/{graph_id}/revokeSignInSessions", operation_name="OFFBOARD_REVOKE_SESSIONS")
                sessions_revoked = True
            except Exception as exc:
                err = f"revoke_sessions: {sanitize(exc)}"
                mutation_errors.append(err)
                LOGGER.warning("Revoking sessions for %s failed: %s", sanitize(upn_or_id), sanitize(exc))

        # 3. Disable Sign-in (Idempotent check)
        signin_disabled = False
        if pre_state.get("accountEnabled") is not False:
            try:
                self.client.patch(f"users/{graph_id}", json_data={"accountEnabled": False}, operation_name="OFFBOARD_DISABLE_SIGNIN")
                signin_disabled = True
            except Exception as exc:
                mutation_errors.append(f"disable_signin: {sanitize(exc)}")
                LOGGER.warning("Disabling sign-in for %s failed: %s", sanitize(upn_or_id), sanitize(exc))
        else:
            signin_disabled = True  # Already disabled

        # 4. Remove Removable Groups (Preserving Protected Groups and NON-LAB groups)
        removed_group_ids: List[str] = []
        for g in removable_groups:
            gid = g["id"]
            try:
                self.client.delete(f"groups/{gid}/members/{graph_id}/$ref", operation_name="OFFBOARD_REMOVE_GROUP")
                removed_group_ids.append(gid)
            except Exception as exc:
                mutation_errors.append(f"remove_group:{gid}: {sanitize(exc)}")
                LOGGER.warning("Removing group %s for %s failed: %s", gid, sanitize(upn_or_id), sanitize(exc))

        # 5. Remove configured removable assigned licenses.
        removed_sku_ids: List[str] = []
        if removable_sku_ids:
            try:
                lic_payload = {"addLicenses": [], "removeLicenses": removable_sku_ids}
                self.client.post(f"users/{graph_id}/assignLicense", json_data=lic_payload, operation_name="OFFBOARD_REMOVE_LICENSES")
                removed_sku_ids = removable_sku_ids
            except Exception as exc:
                mutation_errors.append(f"remove_licenses: {sanitize(exc)}")
                LOGGER.warning("Removing licenses for %s failed: %s", sanitize(upn_or_id), sanitize(exc))

        # 6. Read-Back Verification
        post_state = self._capture_state(graph_id)
        post_group_ids = {g.get("id") for g in post_state.get("groups", [])}
        post_sku_ids = {lic.get("skuId") for lic in post_state.get("assignedLicenses", []) if lic.get("skuId")}
        removed_groups_absent = all(g["id"] not in post_group_ids for g in removable_groups)
        preserved_groups_present = all(gid in post_group_ids for gid in preserved_group_ids)
        removed_skus_absent = all(sku not in post_sku_ids for sku in removable_sku_ids)
        preserved_skus_present = all(sku in post_sku_ids for sku in preserved_sku_ids)
        signin_verified = post_state.get("accountEnabled") is False
        verification_report = {
            "signin_disabled": signin_verified,
            "sessions_revoked": sessions_revoked,
            "sessions_revoke_skipped": not offboarding_needed,
            "removed_groups_absent": removed_groups_absent,
            "preserved_groups_present": preserved_groups_present,
            "removed_skus_absent": removed_skus_absent,
            "preserved_skus_present": preserved_skus_present,
            "user_deleted": False,
            "mutation_errors": mutation_errors,
        }
        verification_passed = all(
            [
                signin_verified,
                removed_groups_absent,
                preserved_groups_present,
                removed_skus_absent,
                preserved_skus_present,
                not mutation_errors,
                not post_state.get("error"),
            ]
        )

        return OffboardingResult(
            upn=upn_or_id,
            graph_id=graph_id,
            sessions_revoked=sessions_revoked,
            signin_disabled=signin_disabled or (post_state.get("accountEnabled") is False),
            removed_group_ids=removed_group_ids,
            preserved_group_ids=preserved_group_ids,
            removed_sku_ids=removed_sku_ids,
            preserved_sku_ids=preserved_sku_ids,
            pre_offboarding_state=pre_state,
            post_offboarding_state=post_state,
            verification_passed=verification_passed,
            verification_report=verification_report,
            status="success" if verification_passed else "partial",
            error_details="; ".join(mutation_errors),
        )

    def _removable_groups(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        groups: List[Dict[str, Any]] = []
        for group in state.get("groups", []):
            gid = group.get("id")
            if not gid or gid in self.protected_group_ids:
                continue
            if (group.get("displayName") or "").upper().startswith("LAB-"):
                groups.append(group)
        return groups

    def _preserved_group_ids(self, state: Dict[str, Any]) -> List[str]:
        preserved: List[str] = []
        for group in state.get("groups", []):
            gid = group.get("id")
            if not gid:
                continue
            if gid in self.protected_group_ids or not (group.get("displayName") or "").upper().startswith("LAB-"):
                preserved.append(gid)
        return preserved

    def _split_removable_skus(self, state: Dict[str, Any]) -> tuple[List[str], List[str]]:
        assigned = [lic["skuId"] for lic in state.get("assignedLicenses", []) if lic.get("skuId")]
        if self.removable_sku_ids is None:
            return assigned, []
        removable = [sku for sku in assigned if sku in self.removable_sku_ids]
        preserved = [sku for sku in assigned if sku not in self.removable_sku_ids]
        return removable, preserved

    def sync_to_odoo(self, odoo_client: Any, result: OffboardingResult) -> Dict[str, Any]:
        """Persist synthetic offboarding result to Odoo control plane."""
        if not odoo_client:
            return {"status": "skipped", "reason": "No Odoo client provided"}

        import json

        summary_msg = (
            f"Synthetic Employee Offboarding ({result.status.upper()})\n"
            f"UPN: {result.upn} | Graph ID: {result.graph_id}\n"
            f"Sign-in Disabled: {result.signin_disabled} | Sessions Revoked: {result.sessions_revoked}\n"
            f"Removed Groups: {len(result.removed_group_ids)} | Removed SKUs: {len(result.removed_sku_ids)}"
        )
        notes_payload = result.to_dict()
        notes_json = json.dumps(notes_payload, indent=2, ensure_ascii=False)

        try:
            if odoo_client.model_exists("cs.m365.operation"):
                # -- Primary path: cs.m365.operation (self-hosted Odoo with addon) --
                ts_str = result.offboarded_at.replace("T", " ")[:19] if result.offboarded_at else False
                state = "verified" if result.verification_passed else "failed"
                op_vals = {
                    "operation_type": "offboarding",
                    "target_upn": result.upn,
                    "target_graph_id": result.graph_id or "",
                    "created_at": ts_str,
                    "started_at": ts_str,
                    "finished_at": ts_str,
                    "state": state,
                    "result": (
                        f"Sign-in disabled: {result.signin_disabled} | "
                        f"Groups removed: {len(result.removed_group_ids)} | "
                        f"SKUs removed: {len(result.removed_sku_ids)}"
                    ),
                    "error_details": sanitize(result.error_details) if result.error_details else False,
                    "notes": notes_json,
                }
                existing = odoo_client.search_read(
                    "cs.m365.operation",
                    [["target_upn", "=", result.upn], ["operation_type", "=", "offboarding"]],
                    fields=["id"],
                    limit=1,
                )
                if existing:
                    odoo_client.write("cs.m365.operation", [existing[0]["id"]], op_vals)
                    rec_id = existing[0]["id"]
                else:
                    op_vals["name"] = f"M365-OFFBOARD-{result.upn}"
                    rec_id = odoo_client.create_one("cs.m365.operation", op_vals)

                LOGGER.info("Persisted Offboarding Result for %s to cs.m365.operation (id=%s)", result.upn, rec_id)
                return {"status": "success", "model": "cs.m365.operation", "upn": result.upn, "record_id": rec_id}

            elif odoo_client.model_exists("x_m365_operation"):
                # -- Dedicated Studio model: x_m365_operation (Odoo Online) --
                ts_str = result.offboarded_at.replace("T", " ")[:19] if result.offboarded_at else False
                state = "verified" if result.verification_passed else ("planned" if result.status == "dry_run" else "failed")
                op_name = f"M365-OFFBOARD-{result.upn}"
                op_vals = {
                    "x_name": op_name,
                    "x_operation_type": "offboarding",
                    "x_target_upn": result.upn,
                    "x_target_graph_id": result.graph_id or "",
                    "x_state": state,
                    "x_execution_result": (
                        f"Sign-in disabled: {result.signin_disabled} | "
                        f"Sessions revoked: {result.sessions_revoked} | "
                        f"Groups removed: {len(result.removed_group_ids)} | "
                        f"SKUs removed: {len(result.removed_sku_ids)}"
                    ),
                    "x_verification_passed": result.verification_passed,
                    "x_error_details": sanitize(result.error_details) if result.error_details else False,
                    "x_created_at": ts_str,
                    "x_finished_at": ts_str,
                    "x_notes": notes_json,
                }
                existing = odoo_client.search_read(
                    "x_m365_operation",
                    [["x_target_upn", "=", result.upn], ["x_operation_type", "=", "offboarding"]],
                    fields=["id"],
                    limit=1,
                )
                if existing:
                    odoo_client.write("x_m365_operation", [existing[0]["id"]], op_vals)
                    rec_id = existing[0]["id"]
                else:
                    rec_id = odoo_client.create_one("x_m365_operation", op_vals)

                LOGGER.info("Persisted Offboarding Result for %s to x_m365_operation (id=%s)", result.upn, rec_id)
                return {"status": "success", "model": "x_m365_operation", "upn": result.upn, "record_id": rec_id}

            elif odoo_client.model_exists("x_integration_config"):
                # -- Fallback: x_integration_config (Odoo Online without addon) --
                config_name = f"Microsoft 365 Offboarding: {result.upn}"
                last_check_str = result.offboarded_at.replace("T", " ")[:19] if result.offboarded_at else False
                vals = {
                    "x_name": config_name,
                    "x_provider": "m365_graph",
                    "x_sync_state": "done" if result.verification_passed else "failed",
                    "x_last_sync_at": last_check_str,
                    "x_notes": notes_json,
                    "x_sync_message": summary_msg,
                    "x_active": True,
                }
                existing = odoo_client.search_read("x_integration_config", [["x_name", "=", config_name]], fields=["id"], limit=1)
                if existing:
                    odoo_client.write("x_integration_config", [existing[0]["id"]], vals)
                    rec_id = existing[0]["id"]
                else:
                    rec_id = odoo_client.create_one("x_integration_config", vals)

                LOGGER.info("Persisted Offboarding Result for %s to Odoo Online (x_integration_config id=%s)", result.upn, rec_id)
                return {"status": "success", "model": "x_integration_config", "upn": result.upn, "record_id": rec_id}
            else:
                return {"status": "skipped", "reason": "No compatible Odoo model accessible"}

        except Exception as exc:
            sanitized_err = sanitize(exc)
            LOGGER.warning("Could not persist Offboarding Result to Odoo Online: %s", sanitized_err)
            return {"status": "fallback", "reason": sanitized_err}


__all__ = ["OffboardingService", "OffboardingResult"]
