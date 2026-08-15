# -*- coding: utf-8 -*-
"""Microsoft 365 User 360 / Helpdesk Diagnostic Service.

Collects Graph-powered user diagnostic snapshots (Identity, Object ID, Account State,
Profile, Manager, Assigned Licenses, Service Plans, Direct & Transitive Groups,
Auth Methods, and Device Info) with graceful degradation for unsupported endpoints.
Read-only diagnostic service — zero Graph mutations performed.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote
from typing import Any, Dict, List, Optional

from .clients.ms_graph_client import MSGraphClient
from .clients.tenant_context import TenantContext
from .clients.token_provider import TokenProvider
from .errors import AuthorizationError, ClientError, GraphProtocolError, HttpError
from .sanitize import sanitize

LOGGER = logging.getLogger("integration_service.ms_graph.user_360")


@dataclass
class User360Snapshot:
    """Timestamped snapshot of a User 360 diagnostic."""

    upn: str
    graph_id: str
    display_name: str
    account_enabled: bool
    job_title: str
    department: str
    usage_location: str
    manager: Dict[str, Any] = field(default_factory=dict)
    assigned_licenses: List[Dict[str, Any]] = field(default_factory=list)
    service_plans: List[Dict[str, Any]] = field(default_factory=list)
    direct_groups: List[Dict[str, Any]] = field(default_factory=list)
    transitive_groups: List[Dict[str, Any]] = field(default_factory=list)
    auth_methods: Any = "Not available"
    devices: Any = "Not available"
    availability: Dict[str, str] = field(default_factory=dict)
    snapshot_timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    diagnostic_status: str = "success"  # "success", "partial", "failed"
    error_details: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class User360Service:
    """Read-only User 360 diagnostic service."""

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

    @staticmethod
    def _user_path_key(upn_or_email: str) -> str:
        """Return a Graph-safe users/{key} path segment for UPN/email lookup."""
        return quote(upn_or_email, safe="@.")

    @staticmethod
    def _availability_from_error(exc: Exception) -> str:
        if isinstance(exc, AuthorizationError):
            return "Not available - permission denied"
        if isinstance(exc, ClientError) and exc.status_code in (400, 404):
            return "Not available"
        return f"Not available - {sanitize(exc)}"

    def get_user_snapshot(self, upn_or_email: str) -> User360Snapshot:
        """Collect User 360 diagnostic snapshot for a given UPN or email."""
        LOGGER.info("Collecting User 360 diagnostic for UPN/Email: %s", sanitize(upn_or_email))
        
        # 1. Primary User Profile
        user_key = self._user_path_key(upn_or_email)
        user_path = f"users/{user_key}?$select=id,userPrincipalName,displayName,accountEnabled,jobTitle,department,usageLocation,assignedLicenses,assignedPlans"
        try:
            res = self.client.get(user_path, operation_name="USER_360_GET_PROFILE")
            u_data = res.data if isinstance(res.data, dict) else {}
        except Exception as exc:
            sanitized_err = sanitize(exc)
            LOGGER.error("User 360 failed to fetch profile for %s: %s", sanitize(upn_or_email), sanitized_err)
            return User360Snapshot(
                upn=upn_or_email,
                graph_id="unknown",
                display_name="Unknown",
                account_enabled=False,
                job_title="",
                department="",
                usage_location="",
                diagnostic_status="failed",
                error_details=sanitized_err,
            )

        graph_id = u_data.get("id", "")
        display_name = u_data.get("displayName", "")
        account_enabled = bool(u_data.get("accountEnabled", False))
        job_title = u_data.get("jobTitle") or ""
        department = u_data.get("department") or ""
        usage_location = u_data.get("usageLocation") or ""
        assigned_licenses = u_data.get("assignedLicenses") or []
        service_plans = u_data.get("assignedPlans") or []
        availability: Dict[str, str] = {
            "profile": "Available",
            "manager": "Available",
            "direct_groups": "Available",
            "transitive_groups": "Available",
            "auth_methods": "Available",
            "registered_devices": "Available",
            "managed_devices": "Available",
        }

        # 2. Manager Info
        manager_info: Dict[str, Any] = {}
        try:
            mgr_res = self.client.get(f"users/{graph_id}/manager", operation_name="USER_360_GET_MANAGER")
            if isinstance(mgr_res.data, dict):
                manager_info = {
                    "id": mgr_res.data.get("id"),
                    "displayName": mgr_res.data.get("displayName"),
                    "userPrincipalName": mgr_res.data.get("userPrincipalName"),
                }
        except Exception as exc:
            availability["manager"] = self._availability_from_error(exc)
            manager_info = {"status": "Not assigned / Not available"}

        # 3. Direct Group Memberships
        direct_groups: List[Dict[str, Any]] = []
        try:
            for g in self.client.paginate(f"users/{graph_id}/memberOf", operation_name="USER_360_GET_MEMBER_OF"):
                if g.get("@odata.type") == "#microsoft.graph.group":
                    direct_groups.append({
                        "id": g.get("id"),
                        "displayName": g.get("displayName"),
                        "groupTypes": g.get("groupTypes", []),
                    })
        except Exception as exc:
            availability["direct_groups"] = self._availability_from_error(exc)
            LOGGER.warning("Could not fetch direct groups for %s: %s", sanitize(upn_or_email), sanitize(exc))

        # 4. Transitive Group Memberships
        transitive_groups: List[Dict[str, Any]] = []
        try:
            for g in self.client.paginate(f"users/{graph_id}/transitiveMemberOf", operation_name="USER_360_GET_TRANSITIVE_MEMBER_OF"):
                if g.get("@odata.type") == "#microsoft.graph.group":
                    transitive_groups.append({
                        "id": g.get("id"),
                        "displayName": g.get("displayName"),
                        "groupTypes": g.get("groupTypes", []),
                    })
        except Exception as exc:
            availability["transitive_groups"] = self._availability_from_error(exc)
            LOGGER.warning("Could not fetch transitive groups for %s: %s", sanitize(upn_or_email), sanitize(exc))

        # 5. Auth Methods (Graceful degradation)
        auth_methods: Any = "Not available"
        try:
            am_res = self.client.get(f"users/{graph_id}/authentication/methods", operation_name="USER_360_GET_AUTH_METHODS")
            if isinstance(am_res.data, dict) and "value" in am_res.data:
                auth_methods = am_res.data["value"]
        except Exception as exc:
            availability["auth_methods"] = self._availability_from_error(exc)
            auth_methods = "Not available"

        # 6. Registered and managed devices (Graceful degradation)
        registered_devices: Any = "Not available"
        try:
            dev_res = self.client.get(f"users/{graph_id}/registeredDevices", operation_name="USER_360_GET_REGISTERED_DEVICES")
            if isinstance(dev_res.data, dict) and "value" in dev_res.data:
                registered_devices = dev_res.data["value"]
        except Exception as exc:
            availability["registered_devices"] = self._availability_from_error(exc)

        managed_devices: Any = "Not available"
        try:
            md_res = self.client.get(
                "deviceManagement/managedDevices",
                params={"$filter": f"userPrincipalName eq '{upn_or_email}'"},
                operation_name="USER_360_GET_MANAGED_DEVICES",
            )
            if isinstance(md_res.data, dict) and "value" in md_res.data:
                managed_devices = md_res.data["value"]
        except Exception as exc:
            availability["managed_devices"] = self._availability_from_error(exc)

        devices: Any = {
            "registered": registered_devices,
            "managed": managed_devices,
        }

        diagnostic_status = "partial" if any(v != "Available" for k, v in availability.items() if k != "manager") else "success"
        unavailable = {k: v for k, v in availability.items() if v != "Available"}
        error_details = ""
        if unavailable:
            import json as _json
            error_details = _json.dumps(unavailable, ensure_ascii=False)

        return User360Snapshot(
            upn=u_data.get("userPrincipalName") or upn_or_email,
            graph_id=graph_id,
            display_name=display_name,
            account_enabled=account_enabled,
            job_title=job_title,
            department=department,
            usage_location=usage_location,
            manager=manager_info,
            assigned_licenses=assigned_licenses,
            service_plans=service_plans,
            direct_groups=direct_groups,
            transitive_groups=transitive_groups,
            auth_methods=auth_methods,
            devices=devices,
            availability=availability,
            diagnostic_status=diagnostic_status,
            error_details=error_details,
        )

    def sync_to_odoo(self, odoo_client: Any, snapshot: User360Snapshot) -> Dict[str, Any]:
        """Persist User 360 snapshot to Odoo Online audit log / control plane."""
        if not odoo_client:
            return {"status": "skipped", "reason": "No Odoo client provided"}

        import json
        from .sync_logger import SyncLogWriter
        from .sync_result import SyncResult

        def supported_vals(model: str, vals: Dict[str, Any]) -> Dict[str, Any]:
            try:
                fields = odoo_client.fields_get(model)
            except Exception:
                return vals
            if not isinstance(fields, dict) or not fields:
                return vals
            return {key: value for key, value in vals.items() if key in fields}

        notes_payload = snapshot.to_dict()
        notes_json = json.dumps(notes_payload, indent=2, ensure_ascii=False)

        summary_msg = (
            f"User 360 Diagnostic for {snapshot.upn} ({snapshot.display_name})\n"
            f"Graph ID: {snapshot.graph_id} | Account Enabled: {snapshot.account_enabled}\n"
            f"Title: '{snapshot.job_title}' | Department: '{snapshot.department}'\n"
            f"Direct Groups: {len(snapshot.direct_groups)} | Transitive Groups: {len(snapshot.transitive_groups)}"
        )

        try:
            if odoo_client.model_exists("cs.m365.user.snapshot"):
                # -- Primary path: cs.m365.user.snapshot (self-hosted Odoo with addon) --
                import json as _json
                snapshot_ts = snapshot.snapshot_timestamp
                if snapshot_ts and "T" in snapshot_ts:
                    snapshot_ts_odoo = snapshot_ts.replace("T", " ")[:19]
                else:
                    snapshot_ts_odoo = snapshot_ts

                # Manager UPN
                manager_upn = ""
                if isinstance(snapshot.manager, dict):
                    manager_upn = snapshot.manager.get("userPrincipalName") or snapshot.manager.get("displayName") or ""

                # Auth methods status summary
                if isinstance(snapshot.auth_methods, list):
                    auth_status = f"{len(snapshot.auth_methods)} method(s) registered"
                else:
                    auth_status = str(snapshot.auth_methods or "Not available")

                # Devices count
                devices_count = 0
                if isinstance(snapshot.devices, list):
                    devices_count = len(snapshot.devices)
                elif isinstance(snapshot.devices, dict):
                    for value in snapshot.devices.values():
                        if isinstance(value, list):
                            devices_count += len(value)

                snap_vals = {
                    "name": f"{snapshot.upn} ({snapshot_ts_odoo})",
                    "upn": snapshot.upn,
                    "display_name": snapshot.display_name,
                    "graph_id": snapshot.graph_id,
                    "account_enabled": snapshot.account_enabled,
                    "job_title": snapshot.job_title or "",
                    "department": snapshot.department or "",
                    "usage_location": snapshot.usage_location or "",
                    "manager_upn": manager_upn,
                    "assigned_licenses": _json.dumps(snapshot.assigned_licenses, ensure_ascii=False),
                    "direct_groups_count": len(snapshot.direct_groups),
                    "transitive_groups_count": len(snapshot.transitive_groups),
                    "auth_methods_status": auth_status,
                    "devices_count": devices_count,
                    "snapshot_timestamp": snapshot_ts_odoo,
                    "diagnostic_status": snapshot.diagnostic_status,
                    "error_details": snapshot.error_details or False,
                    "notes": notes_json,
                }
                snap_vals = supported_vals("cs.m365.user.snapshot", snap_vals)
                rec_id = odoo_client.create_one("cs.m365.user.snapshot", snap_vals)

                LOGGER.info(
                    "Persisted User 360 Snapshot for %s to cs.m365.user.snapshot (id=%s)",
                    snapshot.upn, rec_id,
                )
                return {"status": "success", "model": "cs.m365.user.snapshot", "upn": snapshot.upn, "record_id": rec_id}

            elif odoo_client.model_exists("x_m365_user_snapshot"):
                # -- Dedicated Studio model: x_m365_user_snapshot (Odoo Online) --
                import json as _json
                last_check_str = snapshot.snapshot_timestamp.replace("T", " ")[:19] if snapshot.snapshot_timestamp else False
                mgr_str = ""
                if isinstance(snapshot.manager, dict):
                    mgr_str = snapshot.manager.get("displayName") or snapshot.manager.get("userPrincipalName") or ""
                else:
                    mgr_str = str(snapshot.manager or "")

                groups_payload = {
                    "direct": snapshot.direct_groups,
                    "transitive": snapshot.transitive_groups,
                }
                devices_count = 0
                if isinstance(snapshot.devices, list):
                    devices_count = len(snapshot.devices)
                elif isinstance(snapshot.devices, dict):
                    for value in snapshot.devices.values():
                        if isinstance(value, list):
                            devices_count += len(value)
                snap_vals = {
                    "x_name": f"{snapshot.upn} ({last_check_str or 'Snapshot'})",
                    "x_upn": snapshot.upn,
                    "x_display_name": snapshot.display_name,
                    "x_graph_id": snapshot.graph_id,
                    "x_account_enabled": snapshot.account_enabled,
                    "x_job_title": snapshot.job_title or "",
                    "x_department": snapshot.department or "",
                    "x_usage_location": snapshot.usage_location or "",
                    "x_manager": mgr_str,
                    "x_assigned_licenses": _json.dumps(snapshot.assigned_licenses, ensure_ascii=False),
                    "x_group_memberships": _json.dumps(groups_payload, ensure_ascii=False),
                    "x_auth_methods": str(snapshot.auth_methods or "Not available"),
                    "x_devices_count": devices_count,
                    "x_snapshot_timestamp": last_check_str,
                    "x_diagnostic_status": snapshot.diagnostic_status,
                    "x_error_details": snapshot.error_details or False,
                    "x_notes": notes_json,
                }
                snap_vals = supported_vals("x_m365_user_snapshot", snap_vals)
                rec_id = odoo_client.create_one("x_m365_user_snapshot", snap_vals)
                LOGGER.info("Persisted User 360 Snapshot for %s to x_m365_user_snapshot (id=%s)", snapshot.upn, rec_id)
                return {"status": "success", "model": "x_m365_user_snapshot", "upn": snapshot.upn, "record_id": rec_id}

            elif odoo_client.model_exists("x_integration_config"):
                # -- Fallback: x_integration_config (Odoo Online without addon) --
                config_name = f"Microsoft 365 User 360: {snapshot.upn}"
                last_check_str = snapshot.snapshot_timestamp.replace("T", " ")[:19] if snapshot.snapshot_timestamp else False
                vals = {
                    "x_name": config_name,
                    "x_provider": "m365_graph",
                    "x_sync_state": "done" if snapshot.diagnostic_status in ("success", "partial") else "failed",
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

                LOGGER.info("Persisted User 360 Diagnostic for %s to Odoo Online (x_integration_config id=%s)", snapshot.upn, rec_id)
                return {"status": "success", "model": "x_integration_config", "upn": snapshot.upn, "record_id": rec_id}
            else:
                return {"status": "skipped", "reason": "No compatible Odoo model accessible"}

        except Exception as exc:
            sanitized_err = sanitize(exc)
            LOGGER.warning("Could not persist User 360 Snapshot to Odoo Online: %s", sanitized_err)
            return {"status": "fallback", "reason": sanitized_err}


def compare_snapshots(snap1: Dict[str, Any], snap2: Dict[str, Any], odoo_client: Optional[Any] = None) -> Dict[str, Any]:
    """Compare two User 360 snapshot records for field-by-field diff, and optionally persist to Odoo Online."""
    upn = snap1.get("upn") or snap2.get("upn") or snap1.get("x_upn") or snap2.get("x_upn") or "Unknown"
    ts1 = str(snap1.get("snapshot_timestamp") or snap1.get("x_snapshot_timestamp") or "Snapshot 1")
    ts2 = str(snap2.get("snapshot_timestamp") or snap2.get("x_snapshot_timestamp") or "Snapshot 2")

    import json as _json

    # Helper to parse licenses
    def parse_lics(data):
        raw = data.get("assigned_licenses") or data.get("x_assigned_licenses") or []
        if isinstance(raw, str):
            try:
                raw = _json.loads(raw)
            except Exception:
                raw = []
        return {item.get("skuId") for item in raw if isinstance(item, dict) and item.get("skuId")}

    # Helper to parse groups
    def parse_groups(data):
        raw = data.get("direct_groups") or data.get("x_group_memberships") or []
        if isinstance(raw, str):
            try:
                parsed = _json.loads(raw)
                if isinstance(parsed, dict) and "direct" in parsed:
                    raw = list(parsed.get("direct") or []) + list(parsed.get("transitive") or [])
                elif isinstance(parsed, list):
                    raw = parsed
                else:
                    raw = []
            except Exception:
                raw = []
        out = set()
        for g in raw:
            if isinstance(g, dict):
                out.add(g.get("displayName") or g.get("id") or "")
        return {g for g in out if g}

    lics1 = parse_lics(snap1)
    lics2 = parse_lics(snap2)
    added_lics = sorted(list(lics2 - lics1))
    removed_lics = sorted(list(lics1 - lics2))

    grps1 = parse_groups(snap1)
    grps2 = parse_groups(snap2)
    added_grps = sorted(list(grps2 - grps1))
    removed_grps = sorted(list(grps1 - grps2))

    field_diffs = []
    
    # Account Enabled
    ae1 = snap1.get("account_enabled") if "account_enabled" in snap1 else snap1.get("x_account_enabled")
    ae2 = snap2.get("account_enabled") if "account_enabled" in snap2 else snap2.get("x_account_enabled")
    if ae1 != ae2:
        field_diffs.append(f"Account Enabled: {ae1} -> {ae2}")

    # Job Title
    jt1 = snap1.get("job_title") or snap1.get("x_job_title") or ""
    jt2 = snap2.get("job_title") or snap2.get("x_job_title") or ""
    if jt1 != jt2:
        field_diffs.append(f"Job Title: '{jt1}' -> '{jt2}'")

    # Department
    dp1 = snap1.get("department") or snap1.get("x_department") or ""
    dp2 = snap2.get("department") or snap2.get("x_department") or ""
    if dp1 != dp2:
        field_diffs.append(f"Department: '{dp1}' -> '{dp2}'")

    # Manager
    mg1 = str(snap1.get("manager") or snap1.get("x_manager") or "")
    mg2 = str(snap2.get("manager") or snap2.get("x_manager") or "")
    if mg1 != mg2:
        field_diffs.append(f"Manager: '{mg1}' -> '{mg2}'")

    summary_lines = [
        f"=== USER 360 SNAPSHOT COMPARISON FOR {upn} ===",
        f"Snapshot 1: {ts1}",
        f"Snapshot 2: {ts2}",
        "--------------------------------------------------",
        f"Field Differences: {len(field_diffs)}",
    ]
    for d in field_diffs:
        summary_lines.append(f"  * {d}")

    summary_lines.append(f"Licenses Added: {len(added_lics)}")
    for l in added_lics:
        summary_lines.append(f"  + SKU: {l}")
    summary_lines.append(f"Licenses Removed: {len(removed_lics)}")
    for l in removed_lics:
        summary_lines.append(f"  - SKU: {l}")

    summary_lines.append(f"Groups Added: {len(added_grps)}")
    for g in added_grps:
        summary_lines.append(f"  + Group: {g}")
    summary_lines.append(f"Groups Removed: {len(removed_grps)}")
    for g in removed_grps:
        summary_lines.append(f"  - Group: {g}")

    summary_text = "\n".join(summary_lines)

    result_dict = {
        "upn": upn,
        "timestamp_1": ts1,
        "timestamp_2": ts2,
        "field_diffs": field_diffs,
        "added_licenses": added_lics,
        "removed_licenses": removed_lics,
        "added_groups": added_grps,
        "removed_groups": removed_grps,
        "summary": summary_text,
    }

    if odoo_client and odoo_client.model_exists("x_m365_snapshot_diff"):
        diff_vals = {
            "x_name": f"Diff for {upn} ({ts1[:10]} vs {ts2[:10]})",
            "x_upn": upn,
            "x_timestamp_1": ts1,
            "x_timestamp_2": ts2,
            "x_field_diffs": "\n".join(field_diffs) if field_diffs else "No field differences",
            "x_added_licenses": "\n".join(added_lics) if added_lics else "None",
            "x_removed_licenses": "\n".join(removed_lics) if removed_lics else "None",
            "x_added_groups": "\n".join(added_grps) if added_grps else "None",
            "x_removed_groups": "\n".join(removed_grps) if removed_grps else "None",
            "x_summary": summary_text,
        }
        try:
            rec_id = odoo_client.create_one("x_m365_snapshot_diff", diff_vals)
            result_dict["odoo_record_id"] = rec_id
            LOGGER.info("Persisted Snapshot Diff to x_m365_snapshot_diff (id=%s)", rec_id)
        except Exception as exc:
            LOGGER.warning("Could not persist Snapshot Diff to Odoo: %s", exc)

    return result_dict


__all__ = ["User360Service", "User360Snapshot", "compare_snapshots"]
