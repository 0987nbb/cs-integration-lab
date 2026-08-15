# -*- coding: utf-8 -*-
"""Synthetic Employee Onboarding Service.

Implements two-stage onboarding:
1. Plan / Dry Run: Computes and returns exact planned Graph mutations without modifying M365.
2. Execute & Verify: Executes user creation, profile setup, usage location, manager assignment,
   license assignment, and group membership additions for `LAB-*` synthetic users.
   Reads back state from Graph to perform explicit post-execution verification before declaring success.
"""
from __future__ import annotations

import logging
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .clients.ms_graph_client import MSGraphClient
from .clients.tenant_context import TenantContext
from .clients.token_provider import TokenProvider
from .errors import AuthorizationError, ClientError, GraphProtocolError, HttpError, IntegrationError
from .sanitize import sanitize
from .tenant_readiness import TenantReadinessService

LOGGER = logging.getLogger("integration_service.ms_graph.onboarding")


@dataclass
class OnboardingPlan:
    """Detailed plan showing exact Graph mutations that will occur."""

    upn: str
    display_name: str
    job_title: str
    department: str
    usage_location: str
    manager_upn: Optional[str]
    target_sku_id: Optional[str]
    target_sku_part_number: Optional[str]
    target_group_ids: List[str] = field(default_factory=list)
    target_group_names: List[str] = field(default_factory=list)
    planned_mutations: List[Dict[str, Any]] = field(default_factory=list)
    validation_errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OnboardingResult:
    """Execution and verification result of an onboarding operation."""

    upn: str
    graph_id: str
    stage: str  # "plan", "executed", "verified", "failed"
    plan: OnboardingPlan
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    verification_passed: bool = False
    verified_properties: Dict[str, Any] = field(default_factory=dict)
    error_details: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class OnboardingService:
    """Service that manages synthetic employee onboarding."""

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

    def plan_onboarding(
        self,
        first_name: str,
        last_name: str,
        job_title: str = "Software Engineer",
        department: str = "Engineering",
        usage_location: str = "US",
        manager_upn: Optional[str] = None,
        desired_sku_part_number: Optional[str] = None,
    ) -> OnboardingPlan:
        """Stage 1: Generate dry-run onboarding plan without executing any M365 mutations."""
        readiness_svc = TenantReadinessService(graph_client=self.client)
        ident = readiness_svc.discover_tenant_identity()
        domain = ident.get("primary_domain") or "demo.onmicrosoft.com"

        # Ensure synthetic user adheres to LAB- naming rule
        safe_name = f"LAB-User-{first_name}-{last_name}".replace(" ", "")
        upn = f"{safe_name.lower()}@{domain}"
        if not manager_upn:
            try:
                users_resp = self.client.get("users?$top=1&$select=userPrincipalName", operation_name="ONBOARD_DISCOVER_MANAGER")
                users = users_resp.data.get("value", []) if isinstance(users_resp.data, dict) else []
                if users:
                    manager_upn = users[0].get("userPrincipalName")
            except Exception:
                pass

        display_name = f"{safe_name} ({job_title})"
        # Discover licenses and groups dynamically
        sku_res = readiness_svc.discover_skus()
        target_sku_id = None
        target_sku_part = None
        for sku in sku_res.get("skus", []):
            if sku.get("available_units", 0) > 0:
                if desired_sku_part_number is None or sku.get("sku_part_number") == desired_sku_part_number:
                    target_sku_id = sku.get("sku_id")
                    target_sku_part = sku.get("sku_part_number")
                    break
        if not target_sku_id and sku_res.get("skus"):
            target_sku_id = sku_res["skus"][0].get("sku_id")
            target_sku_part = sku_res["skus"][0].get("sku_part_number")

        lab_groups = readiness_svc.discover_lab_groups(prefix="LAB-")
        target_group_ids = [g["graph_id"] for g in lab_groups[:2]]
        target_group_names = [g["name"] for g in lab_groups[:2]]
        validation_errors: List[str] = []
        if not manager_upn:
            validation_errors.append("No manager UPN was provided or discovered.")
        if not target_sku_id:
            validation_errors.append("No available Microsoft 365 license SKU was discovered.")
        if len(target_group_ids) < 2:
            validation_errors.append("Fewer than two LAB-* demo groups were discovered.")

        planned_mutations = [
            {
                "step": 1,
                "action": "CREATE_USER",
                "method": "POST",
                "path": "users",
                "payload": {
                    "accountEnabled": True,
                    "displayName": display_name,
                    "mailNickname": safe_name.lower(),
                    "userPrincipalName": upn,
                    "passwordProfile": {"forceChangePasswordNextSignIn": True, "password": "[REDACTED_TEMP_PASS]"},
                    "jobTitle": job_title,
                    "department": department,
                    "usageLocation": usage_location,
                },
            },
            {
                "step": 2,
                "action": "ASSIGN_MANAGER",
                "method": "PUT",
                "path": f"users/{upn}/manager/$ref",
                "payload": {"@odata.id": f"{self.client.tenant_context.graph_base_url}/users/{manager_upn}" if manager_upn else "N/A"},
            },
            {
                "step": 3,
                "action": "ASSIGN_LICENSE",
                "method": "POST",
                "path": f"users/{upn}/assignLicense",
                "payload": {"addLicenses": [{"skuId": target_sku_id}], "removeLicenses": []} if target_sku_id else "No license available",
            },
        ]
        for idx, gid in enumerate(target_group_ids, start=4):
            planned_mutations.append({
                "step": idx,
                "action": f"ADD_TO_GROUP_{idx-3}",
                "method": "POST",
                "path": f"groups/{gid}/members/$ref",
                "payload": {"@odata.id": f"{self.client.tenant_context.graph_base_url}/users/{upn}"},
            })

        return OnboardingPlan(
            upn=upn,
            display_name=display_name,
            job_title=job_title,
            department=department,
            usage_location=usage_location,
            manager_upn=manager_upn,
            target_sku_id=target_sku_id,
            target_sku_part_number=target_sku_part,
            target_group_ids=target_group_ids,
            target_group_names=target_group_names,
            planned_mutations=planned_mutations,
            validation_errors=validation_errors,
        )

    @staticmethod
    def plan_from_dict(data: Dict[str, Any]) -> OnboardingPlan:
        allowed = set(OnboardingPlan.__dataclass_fields__.keys())
        return OnboardingPlan(**{key: value for key, value in data.items() if key in allowed})

    def plan_result(self, plan: OnboardingPlan) -> OnboardingResult:
        """Represent a dry-run plan as an Odoo-storable operation result."""
        return OnboardingResult(
            upn=plan.upn,
            graph_id="",
            stage="planned" if not plan.validation_errors else "failed",
            plan=plan,
            verification_passed=False,
            verified_properties={
                "planned_mutation_count": len(plan.planned_mutations),
                "target_group_count": len(plan.target_group_ids),
                "target_groups": plan.target_group_names,
                "target_sku_part_number": plan.target_sku_part_number,
                "manager_upn": plan.manager_upn,
                "validation_errors": plan.validation_errors,
            },
            error_details="; ".join(plan.validation_errors),
        )

    def execute_onboarding(
        self,
        plan: OnboardingPlan,
        temp_password: str = "LAB#TempP@ss2026!",
        approved: bool = False,
    ) -> OnboardingResult:
        """Stage 2: Execute onboarding plan and perform read-back verification."""
        LOGGER.info("Executing synthetic onboarding for UPN: %s", plan.upn)

        # 0. Safety Boundary Enforcement
        if not (plan.upn.startswith("LAB-") or "lab-" in plan.upn.lower()):
            from .errors import InvalidPayloadError
            raise InvalidPayloadError(
                f"Safety Violation: Target UPN '{plan.upn}' does not match the synthetic LAB-* naming convention."
            )

        validation_errors = list(plan.validation_errors)
        if not plan.manager_upn:
            validation_errors.append("Manager assignment is required for onboarding.")
        if not plan.target_sku_id:
            # Re-discover SKU at execute time in case plan was generated before fix
            try:
                from .tenant_readiness import TenantReadinessService
                readiness_svc = TenantReadinessService(graph_client=self.client)
                sku_res = readiness_svc.discover_skus()
                for sku in sku_res.get("skus", []):
                    if sku.get("available_units", 0) > 0:
                        plan.target_sku_id = sku.get("sku_id")
                        plan.target_sku_part_number = sku.get("sku_part_number")
                        LOGGER.info("Re-discovered SKU at execute time: %s", plan.target_sku_part_number)
                        break
                if not plan.target_sku_id and sku_res.get("skus"):
                    plan.target_sku_id = sku_res["skus"][0].get("sku_id")
                    plan.target_sku_part_number = sku_res["skus"][0].get("sku_part_number")
                    LOGGER.info("Fallback SKU at execute time: %s", plan.target_sku_part_number)
            except Exception as sku_exc:
                LOGGER.warning("SKU re-discovery at execute time failed: %s", sanitize(sku_exc))
        if not plan.target_sku_id:
            validation_errors.append("Insufficient license capacity: no available Microsoft 365 SKU was planned.")
        if len(plan.target_group_ids) < 2:
            validation_errors.append("At least two LAB-* demo groups are required for onboarding.")
        if validation_errors:
            return OnboardingResult(
                upn=plan.upn,
                graph_id="",
                stage="failed",
                plan=plan,
                verification_passed=False,
                error_details="; ".join(dict.fromkeys(validation_errors)),
            )

        if not approved:
            return OnboardingResult(
                upn=plan.upn,
                graph_id="",
                stage="unapproved",
                plan=plan,
                verification_passed=False,
                error_details="Operation not explicitly approved. Dry-run only.",
            )

        existing_id = None
        existing_user: Dict[str, Any] = {}
        try:
            ex_res = self.client.get(
                f"users/{plan.upn}?$select=id,userPrincipalName,displayName,accountEnabled,jobTitle,department,usageLocation,assignedLicenses",
                operation_name="ONBOARD_CHECK_EXISTS",
            )
            if isinstance(ex_res.data, dict) and ex_res.data.get("id"):
                existing_id = ex_res.data["id"]
                existing_user = ex_res.data
        except Exception:
            pass

        graph_id = existing_id
        if not graph_id:
            # 1. Create User
            user_payload = {
                "accountEnabled": True,
                "displayName": plan.display_name,
                "mailNickname": plan.upn.split("@")[0],
                "userPrincipalName": plan.upn,
                "passwordProfile": {
                    "forceChangePasswordNextSignIn": True,
                    "password": temp_password,
                },
                "jobTitle": plan.job_title,
                "department": plan.department,
                "usageLocation": plan.usage_location,
            }
            try:
                c_res = self.client.post("users", json_data=user_payload, operation_name="ONBOARD_CREATE_USER")
                graph_id = c_res.data.get("id") if isinstance(c_res.data, dict) else ""
            except Exception as exc:
                sanitized_err = sanitize(exc)
                LOGGER.error("Failed to create user %s: %s", plan.upn, sanitized_err)
                return OnboardingResult(
                    upn=plan.upn,
                    graph_id="",
                    stage="failed",
                    plan=plan,
                    error_details=f"User creation failed: {sanitized_err}",
                )
        else:
            profile_patch: Dict[str, Any] = {}
            expected_profile = {
                "accountEnabled": True,
                "displayName": plan.display_name,
                "jobTitle": plan.job_title,
                "department": plan.department,
                "usageLocation": plan.usage_location,
            }
            for key, expected in expected_profile.items():
                if existing_user.get(key) != expected:
                    profile_patch[key] = expected
            if profile_patch:
                try:
                    self.client.patch(f"users/{graph_id}", json_data=profile_patch, operation_name="ONBOARD_REPAIR_PROFILE")
                except Exception as exc:
                    LOGGER.warning("Repairing existing user profile failed for %s: %s", plan.upn, sanitize(exc))

        current_license_ids = {
            lic.get("skuId")
            for lic in existing_user.get("assignedLicenses", [])
            if isinstance(lic, dict) and lic.get("skuId")
        }
        current_group_ids: List[str] = []
        current_manager_id = ""
        if existing_id and graph_id:
            try:
                for g in self.client.paginate(f"users/{graph_id}/memberOf", operation_name="ONBOARD_CHECK_GROUPS"):
                    if g.get("id"):
                        current_group_ids.append(g["id"])
            except Exception as exc:
                LOGGER.warning("Existing group read failed for %s before retry repair: %s", plan.upn, sanitize(exc))
            try:
                mgr_existing = self.client.get(f"users/{graph_id}/manager?$select=id,userPrincipalName", operation_name="ONBOARD_CHECK_MANAGER")
                if isinstance(mgr_existing.data, dict):
                    current_manager_id = mgr_existing.data.get("id") or ""
            except Exception:
                current_manager_id = ""

        # 2. Assign Manager (if specified)
        manager_id = ""
        if plan.manager_upn and graph_id:
            try:
                # Find manager ID
                m_res = self.client.get(f"users/{plan.manager_upn}?$select=id", operation_name="ONBOARD_GET_MANAGER_ID")
                m_id = m_res.data.get("id") if isinstance(m_res.data, dict) else ""
                if m_id:
                    manager_id = m_id
                    if current_manager_id != m_id:
                        mgr_ref_payload = {"@odata.id": f"{self.client.tenant_context.graph_base_url}/users/{m_id}"}
                        self.client.put(f"users/{graph_id}/manager/$ref", json_data=mgr_ref_payload, operation_name="ONBOARD_SET_MANAGER")
            except Exception as exc:
                LOGGER.warning("Setting manager failed for %s: %s", plan.upn, sanitize(exc))

        # Wait for Graph to propagate new user before assigning license/groups
        import time
        if not existing_id:
            LOGGER.info("Waiting 15s for Graph to propagate new user %s before license/group assignment...", plan.upn)
            time.sleep(15)

        # 3. Assign License (with retry)
        if plan.target_sku_id and graph_id:
            if plan.target_sku_id not in current_license_ids:
                for attempt in range(3):
                    try:
                        lic_payload = {
                            "addLicenses": [{"skuId": plan.target_sku_id}],
                            "removeLicenses": [],
                        }
                        self.client.post(f"users/{graph_id}/assignLicense", json_data=lic_payload, operation_name="ONBOARD_ASSIGN_LICENSE")
                        LOGGER.info("License assigned successfully for %s (attempt %d)", plan.upn, attempt + 1)
                        break
                    except Exception as exc:
                        LOGGER.warning("License assignment attempt %d failed for %s: %s", attempt + 1, plan.upn, sanitize(exc))
                        if attempt < 2:
                            time.sleep(10)

        # 4. Add to Demo LAB Groups (with retry)
        if graph_id:
            for gid in plan.target_group_ids:
                if gid not in current_group_ids:
                    for attempt in range(3):
                        try:
                            grp_ref_payload = {"@odata.id": f"{self.client.tenant_context.graph_base_url}/directoryObjects/{graph_id}"}
                            self.client.post(f"groups/{gid}/members/$ref", json_data=grp_ref_payload, operation_name="ONBOARD_ADD_TO_GROUP")
                            LOGGER.info("Added to group %s successfully (attempt %d)", gid, attempt + 1)
                            break
                        except Exception as exc:
                            LOGGER.warning("Adding to group %s attempt %d failed for %s: %s", gid, attempt + 1, plan.upn, sanitize(exc))
                            if attempt < 2:
                                time.sleep(5)

        # 5. Read-Back Verification Step
        LOGGER.info("Performing post-onboarding Graph read-back verification for %s", plan.upn)
        verification_passed = False
        verified_props: Dict[str, Any] = {}

        try:
            rb_res = self.client.get(
                f"users/{graph_id}?$select=id,userPrincipalName,displayName,accountEnabled,jobTitle,department,usageLocation,assignedLicenses",
                operation_name="ONBOARD_READBACK_VERIFY",
            )
            rb_data = rb_res.data if isinstance(rb_res.data, dict) else {}
            
            # Check direct group memberships
            rb_groups: List[str] = []
            for g in self.client.paginate(f"users/{graph_id}/memberOf", operation_name="ONBOARD_READBACK_GROUPS"):
                if g.get("id"):
                    rb_groups.append(g["id"])

            rb_manager_id = ""
            try:
                mgr_rb = self.client.get(f"users/{graph_id}/manager?$select=id,userPrincipalName", operation_name="ONBOARD_READBACK_MANAGER")
                if isinstance(mgr_rb.data, dict):
                    rb_manager_id = mgr_rb.data.get("id") or ""
            except Exception as exc:
                LOGGER.warning("Manager read-back failed for %s: %s", plan.upn, sanitize(exc))

            verified_props = {
                "exists": bool(rb_data.get("id")),
                "upn_match": rb_data.get("userPrincipalName") == plan.upn,
                "accountEnabled": rb_data.get("accountEnabled"),
                "jobTitle": rb_data.get("jobTitle"),
                "department": rb_data.get("department"),
                "usageLocation": rb_data.get("usageLocation"),
                "assigned_license_count": len(rb_data.get("assignedLicenses", [])),
                "member_group_count": len(rb_groups),
                "manager_id": rb_manager_id,
                "planned_manager_id": manager_id,
            }

            # Strict Verification rule: Verify against all planned attributes
            has_license = any(lic.get("skuId") == plan.target_sku_id for lic in rb_data.get("assignedLicenses", [])) if plan.target_sku_id else True
            has_groups = all(gid in rb_groups for gid in plan.target_group_ids)
            has_manager = bool(rb_manager_id) and (not manager_id or rb_manager_id == manager_id)

            verification_passed = (
                verified_props["exists"] and 
                verified_props["upn_match"] and 
                verified_props["accountEnabled"] and
                verified_props["jobTitle"] == plan.job_title and
                verified_props["department"] == plan.department and
                verified_props["usageLocation"] == plan.usage_location and
                has_manager and
                has_license and
                has_groups
            )

        except Exception as exc:
            sanitized_err = sanitize(exc)
            LOGGER.error("Read-back verification failed for %s: %s", plan.upn, sanitized_err)
            return OnboardingResult(
                upn=plan.upn,
                graph_id=graph_id or "",
                stage="failed",
                plan=plan,
                verification_passed=False,
                error_details=f"Read-back verification failed: {sanitized_err}",
            )

        return OnboardingResult(
            upn=plan.upn,
            graph_id=graph_id or "",
            stage="verified" if verification_passed else "failed",
            plan=plan,
            verification_passed=verification_passed,
            verified_properties=verified_props,
        )

    def sync_to_odoo(self, odoo_client: Any, result: OnboardingResult) -> Dict[str, Any]:
        """Persist onboarding execution and verification status to Odoo control plane."""
        if not odoo_client:
            return {"status": "skipped", "reason": "No Odoo client provided"}

        notes_payload = result.to_dict()
        notes_json = json.dumps(notes_payload, indent=2, ensure_ascii=False)
        full_plan_json = json.dumps(result.plan.to_dict(), ensure_ascii=False)
        summary_msg = (
            f"Synthetic Employee Onboarding ({result.stage.upper()})\n"
            f"UPN: {result.upn} | Graph ID: {result.graph_id}\n"
            f"Verification Passed: {result.verification_passed}\n"
            f"Verified State: {json.dumps(result.verified_properties)}"
        )

        try:
            if odoo_client.model_exists("cs.m365.operation"):
                # -- Primary path: cs.m365.operation (self-hosted Odoo with addon) --
                ts_str = result.created_at.replace("T", " ")[:19] if result.created_at else False
                state = "verified" if result.verification_passed else ("failed" if result.stage == "failed" else "awaiting_approval")
                op_vals = {
                    "operation_type": "onboarding",
                    "target_upn": result.upn,
                    "target_graph_id": result.graph_id or "",
                    "created_at": ts_str,
                    "started_at": ts_str,
                    "finished_at": ts_str,
                    "state": state,
                    "result": json.dumps(result.verified_properties, ensure_ascii=False),
                    "error_details": sanitize(result.error_details) if result.error_details else False,
                    "notes": notes_json,
                }
                existing = odoo_client.search_read(
                    "cs.m365.operation",
                    [["target_upn", "=", result.upn], ["operation_type", "=", "onboarding"]],
                    fields=["id"],
                    limit=1,
                )
                if existing:
                    odoo_client.write("cs.m365.operation", [existing[0]["id"]], op_vals)
                    rec_id = existing[0]["id"]
                else:
                    op_vals["name"] = f"M365-ONBOARD-{result.upn}"
                    rec_id = odoo_client.create_one("cs.m365.operation", op_vals)

                LOGGER.info("Persisted Onboarding Result for %s to cs.m365.operation (id=%s)", result.upn, rec_id)
                return {"status": "success", "model": "cs.m365.operation", "upn": result.upn, "record_id": rec_id}

            elif odoo_client.model_exists("x_m365_operation"):
                # -- Dedicated Studio model: x_m365_operation (Odoo Online) --
                ts_str = result.created_at.replace("T", " ")[:19] if result.created_at else False
                state = "verified" if result.verification_passed else ("failed" if result.stage == "failed" else "awaiting_approval")
                op_name = f"M365-ONBOARD-{result.upn}"
                op_vals = {
                    "x_name": op_name,
                    "x_operation_type": "onboarding",
                    "x_target_upn": result.upn,
                    "x_target_graph_id": result.graph_id or "",
                    "x_state": state,
                    "x_planned_mutations": full_plan_json,
                    "x_execution_result": json.dumps(result.verified_properties, ensure_ascii=False),
                    "x_verification_passed": result.verification_passed,
                    "x_error_details": sanitize(result.error_details) if result.error_details else False,
                    "x_created_at": ts_str,
                    "x_finished_at": ts_str,
                    "x_notes": notes_json,
                }
                existing = odoo_client.search_read(
                    "x_m365_operation",
                    [["x_target_upn", "=", result.upn], ["x_operation_type", "=", "onboarding"]],
                    fields=["id"],
                    limit=1,
                )
                if existing:
                    odoo_client.write("x_m365_operation", [existing[0]["id"]], op_vals)
                    rec_id = existing[0]["id"]
                else:
                    rec_id = odoo_client.create_one("x_m365_operation", op_vals)

                LOGGER.info("Persisted Onboarding Result for %s to x_m365_operation (id=%s)", result.upn, rec_id)
                return {"status": "success", "model": "x_m365_operation", "upn": result.upn, "record_id": rec_id}

            elif odoo_client.model_exists("x_integration_config"):
                # -- Fallback: x_integration_config (Odoo Online without addon) --
                config_name = f"Microsoft 365 Onboarding: {result.upn}"
                last_check_str = result.created_at.replace("T", " ")[:19] if result.created_at else False
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

                LOGGER.info("Persisted Onboarding Result for %s to Odoo Online (x_integration_config id=%s)", result.upn, rec_id)
                return {"status": "success", "model": "x_integration_config", "upn": result.upn, "record_id": rec_id}
            else:
                return {"status": "skipped", "reason": "No compatible Odoo model accessible"}

        except Exception as exc:
            sanitized_err = sanitize(exc)
            LOGGER.warning("Could not persist Onboarding Result to Odoo Online: %s", sanitized_err)
            return {"status": "fallback", "reason": sanitized_err}


__all__ = ["OnboardingService", "OnboardingPlan", "OnboardingResult"]
