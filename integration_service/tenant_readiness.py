# -*- coding: utf-8 -*-
"""Microsoft 365 Tenant Readiness & Discovery Service.

Dynamically discovers Microsoft 365 environment identity, verified domains,
subscribed SKUs, license capacity, LAB test groups, and Graph API capability access
without hard-coded IDs or secret exposure.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .clients.audit_logger import GraphAuditLogger
from .clients.ms_graph_client import MSGraphClient
from .clients.tenant_context import TenantContext
from .clients.token_provider import TokenProvider
from .config import get_settings
from .errors import AuthorizationError, ClientError, GraphProtocolError, HttpError
from .sanitize import sanitize

LOGGER = logging.getLogger("integration_service.ms_graph.readiness")


@dataclass
class TenantReadinessReport:
    """Structured report returned by TenantReadinessService."""

    tenant_id: str
    display_name: str
    primary_domain: str
    domains: List[Dict[str, Any]] = field(default_factory=list)
    skus: List[Dict[str, Any]] = field(default_factory=list)
    lab_groups: List[Dict[str, Any]] = field(default_factory=list)
    capabilities: List[Dict[str, Any]] = field(default_factory=list)
    discovery_errors: List[Dict[str, str]] = field(default_factory=list)
    total_license_capacity: int = 0
    consumed_license_capacity: int = 0
    available_license_capacity: int = 0
    readiness_status: str = "ready"  # "ready", "partial", "failed"
    last_readiness_check: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class TenantReadinessService:
    """Service that executes read-only Microsoft 365 tenant discovery."""

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
        self.discovery_errors: List[Dict[str, str]] = []

    def _record_discovery_error(self, area: str, exc: Exception) -> None:
        self.discovery_errors.append(
            {
                "area": area,
                "status": "denied" if isinstance(exc, AuthorizationError) else "failed",
                "detail": sanitize(exc),
            }
        )

    def discover_tenant_identity(self) -> Dict[str, Any]:
        """Discover organization profile and primary domain dynamically (`GET /organization`)."""
        LOGGER.info("Discovering tenant identity via GET /organization")
        response = self.client.get("organization", operation_name="DISCOVER_ORGANIZATION")
        
        if not isinstance(response.data, dict) or "value" not in response.data:
            raise GraphProtocolError("Invalid response structure from GET /organization")

        orgs = response.data.get("value", [])
        if not orgs:
            raise GraphProtocolError("GET /organization returned an empty organization list")

        org = orgs[0]
        tenant_id = org.get("id", "")
        display_name = org.get("displayName", "")

        primary_domain = ""
        verified_domains = org.get("verifiedDomains", [])
        for vd in verified_domains:
            if vd.get("isDefault"):
                primary_domain = vd.get("name", "")
                break
        if not primary_domain and verified_domains:
            primary_domain = verified_domains[0].get("name", "")

        return {
            "tenant_id": tenant_id,
            "display_name": display_name,
            "primary_domain": primary_domain,
            "verified_domains": verified_domains,
        }

    def discover_domains(self) -> List[Dict[str, Any]]:
        """Discover all registered domains via `GET /domains` using pagination."""
        LOGGER.info("Discovering domains via GET /domains")
        domains: List[Dict[str, Any]] = []

        try:
            for item in self.client.paginate("domains", operation_name="DISCOVER_DOMAINS"):
                domains.append(
                    {
                        "domain_id": item.get("id") or item.get("name", ""),
                        "name": item.get("id") or item.get("name", ""),
                        "is_default": bool(item.get("isDefault")),
                        "is_initial": bool(item.get("isInitial")),
                        "status": "Verified" if item.get("isVerified", True) else "Unverified",
                    }
                )
        except (AuthorizationError, ClientError) as exc:
            self._record_discovery_error("domains", exc)
            LOGGER.warning("Domain discovery failed or forbidden: %s", sanitize(exc))
        return domains

    def discover_skus(self) -> Dict[str, Any]:
        """Discover subscribed SKUs and license capacity (`GET /subscribedSkus`)."""
        LOGGER.info("Discovering subscribed SKUs via GET /subscribedSkus")
        skus: List[Dict[str, Any]] = []
        total_capacity = 0
        total_consumed = 0
        total_available = 0

        try:
            for item in self.client.paginate("subscribedSkus", operation_name="DISCOVER_SKUS"):
                sku_id = item.get("skuId", "")
                sku_part_number = item.get("skuPartNumber", "")
                capability_status = item.get("capabilityStatus", "Enabled")
                consumed_units = item.get("consumedUnits", 0)

                prepaid = item.get("prepaidUnits") or {}
                enabled_units = prepaid.get("enabled", 0)
                available_units = max(0, enabled_units - consumed_units)

                total_capacity += enabled_units
                total_consumed += consumed_units
                total_available += available_units

                skus.append(
                    {
                        "sku_id": sku_id,
                        "sku_part_number": sku_part_number,
                        "capability_status": capability_status,
                        "enabled_units": enabled_units,
                        "consumed_units": consumed_units,
                        "available_units": available_units,
                    }
                )
        except (AuthorizationError, ClientError) as exc:
            self._record_discovery_error("subscribed_skus", exc)
            LOGGER.warning("Subscribed SKUs discovery failed or forbidden: %s", sanitize(exc))

        return {
            "skus": skus,
            "total_capacity": total_capacity,
            "consumed_capacity": total_consumed,
            "available_capacity": total_available,
        }

    def discover_lab_groups(self, prefix: str = "LAB-") -> List[Dict[str, Any]]:
        """Discover configured test/lab groups (`GET /groups`), filtering by prefix."""
        LOGGER.info("Discovering LAB test groups via GET /groups (prefix=%r)", prefix)
        lab_groups: List[Dict[str, Any]] = []

        try:
            for item in self.client.paginate("groups", operation_name="DISCOVER_GROUPS"):
                display_name = item.get("displayName") or ""
                # Enforce safe lab group filtering rule:
                if display_name.upper().startswith(prefix.upper()):
                    lab_groups.append(
                        {
                            "graph_id": item.get("id", ""),
                            "name": display_name,
                            "description": item.get("description") or "",
                            "group_type": ", ".join(item.get("groupTypes") or ["Security"]),
                            "mail_nickname": item.get("mailNickname") or "",
                            "is_lab_group": True,
                        }
                    )
        except (AuthorizationError, ClientError) as exc:
            self._record_discovery_error("lab_groups", exc)
            LOGGER.warning("Group discovery failed or forbidden: %s", sanitize(exc))

        return lab_groups

    def assess_capabilities(self) -> List[Dict[str, Any]]:
        """Test endpoint availability and report permission capability statuses."""
        LOGGER.info("Assessing Graph API endpoint capabilities")
        # Dynamic lookup of a sample user ID for user-scoped capability checks (avoids invalid /me in app-only mode)
        sample_user_id = None
        try:
            u_res = self.client.get("users?$top=1", operation_name="CAPABILITY_GET_SAMPLE_USER")
            if isinstance(u_res.data, dict) and u_res.data.get("value"):
                sample_user_id = u_res.data["value"][0].get("id")
        except Exception:
            pass

        auth_methods_endpoint = f"users/{sample_user_id}/authentication/methods" if sample_user_id else "users"

        capabilities_to_test = [
            ("Organization Profile", "organization", "GET"),
            ("Domain Management", "domains", "GET"),
            ("Subscribed SKUs", "subscribedSkus", "GET"),
            ("Group Read Access", "groups?$top=1", "GET"),
            ("User Read Access", "users?$top=1", "GET"),
            ("Auth Methods Read", auth_methods_endpoint, "GET"),
            ("Managed Devices Read", "deviceManagement/managedDevices?$top=1", "GET"),
        ]
        results: List[Dict[str, Any]] = []

        for name, path, method in capabilities_to_test:
            key = name.lower().replace(" ", "_")
            try:
                self.client.request(method, path, operation_name=f"CAPABILITY_CHECK_{key.upper()}")
                results.append(
                    {
                        "name": name,
                        "capability_key": key,
                        "status": "available",
                        "detail": "Successfully queried Graph endpoint (HTTP 200 OK)",
                    }
                )
            except AuthorizationError as exc:
                results.append(
                    {
                        "name": name,
                        "capability_key": key,
                        "status": "denied",
                        "detail": f"Denied / Insufficient permission: {sanitize(exc)}",
                    }
                )
            except HttpError as exc:
                status_code = exc.status_code or 0
                if status_code in (404, 400):
                    results.append(
                        {
                            "name": name,
                            "capability_key": key,
                            "status": "unsupported",
                            "detail": f"Not available in tenant or unsupported: {sanitize(exc)}",
                        }
                    )
                else:
                    results.append(
                        {
                            "name": name,
                            "capability_key": key,
                            "status": "failed",
                            "detail": f"Capability check failed: {sanitize(exc)}",
                        }
                    )
            except Exception as exc:
                results.append(
                    {
                        "name": name,
                        "capability_key": key,
                        "status": "failed",
                        "detail": f"Capability check error: {sanitize(exc)}",
                    }
                )

        return results

    def run_readiness_check(self, lab_prefix: str = "LAB-") -> TenantReadinessReport:
        """Run complete tenant readiness discovery and produce an aggregated report."""
        LOGGER.info("Starting Microsoft 365 Tenant Readiness Discovery")
        self.discovery_errors = []
        try:
            identity = self.discover_tenant_identity()
            tenant_id = identity["tenant_id"]
            display_name = identity["display_name"]
            primary_domain = identity["primary_domain"]

            domains = self.discover_domains()
            if not primary_domain and domains:
                for d in domains:
                    if d.get("is_default"):
                        primary_domain = d.get("name")
                        break

            sku_data = self.discover_skus()
            lab_groups = self.discover_lab_groups(prefix=lab_prefix)
            capabilities = self.assess_capabilities()

            # Determine overall readiness status
            core_caps = [c for c in capabilities if c["capability_key"] in ("organization_profile", "user_read_access")]
            if any(c["status"] == "denied" for c in core_caps):
                readiness_status = "partial"
            elif self.discovery_errors:
                readiness_status = "partial"
            else:
                readiness_status = "ready"

            report = TenantReadinessReport(
                tenant_id=tenant_id,
                display_name=display_name,
                primary_domain=primary_domain,
                domains=domains,
                skus=sku_data["skus"],
                lab_groups=lab_groups,
                capabilities=capabilities,
                discovery_errors=list(self.discovery_errors),
                total_license_capacity=sku_data["total_capacity"],
                consumed_license_capacity=sku_data["consumed_capacity"],
                available_license_capacity=sku_data["available_capacity"],
                readiness_status=readiness_status,
            )
            LOGGER.info(
                "Tenant Readiness Discovery Completed: tenant_id=%s, domains=%d, skus=%d, lab_groups=%d, status=%s",
                tenant_id,
                len(domains),
                len(sku_data["skus"]),
                len(lab_groups),
                readiness_status,
            )
            return report

        except Exception as exc:
            sanitized_err = sanitize(exc)
            LOGGER.error("Tenant Readiness Discovery failed: %s", sanitized_err)
            return TenantReadinessReport(
                tenant_id=self.client.tenant_context.tenant_id or "unknown",
                display_name="Discovery Failed",
                primary_domain="",
                readiness_status="failed",
                last_error=sanitized_err,
            )

    def sync_to_odoo(self, odoo_client: Any, report: TenantReadinessReport) -> Dict[str, Any]:
        """Persist tenant readiness discovery data to Odoo Online via JSON-2 API.
        
        Uses Odoo Online model x_integration_config (or cs.m365.tenant if available)
        to store complete tenant readiness state idempotently. Also creates an
        audit execution log in x_integration_sync_log.
        """
        if not odoo_client:
            return {"status": "skipped", "reason": "No Odoo client provided"}

        import json
        from .sync_result import SyncResult
        from .sync_logger import SyncLogWriter

        def supported_vals(model: str, vals: Dict[str, Any]) -> Dict[str, Any]:
            try:
                fields = odoo_client.fields_get(model)
            except Exception:
                return vals
            if not isinstance(fields, dict) or not fields:
                return vals
            return {key: value for key, value in vals.items() if key in fields}

        notes_payload = {
            "tenant_id": report.tenant_id,
            "display_name": report.display_name,
            "primary_domain": report.primary_domain,
            "readiness_status": report.readiness_status,
            "last_readiness_check": report.last_readiness_check,
            "license_capacity": {
                "total": report.total_license_capacity,
                "consumed": report.consumed_license_capacity,
                "available": report.available_license_capacity,
            },
            "domains": report.domains,
            "skus": report.skus,
            "lab_groups": report.lab_groups,
            "capabilities": report.capabilities,
            "discovery_errors": report.discovery_errors,
            "last_error": report.last_error,
        }
        notes_json = json.dumps(notes_payload, indent=2, ensure_ascii=False)

        summary_msg = (
            f"Microsoft 365 Tenant Readiness ({report.readiness_status.upper()})\n"
            f"Tenant ID: {report.tenant_id} | Primary Domain: {report.primary_domain}\n"
            f"Domains: {len(report.domains)} | SKUs: {len(report.skus)} | LAB Groups: {len(report.lab_groups)}\n"
            f"License Capacity: {report.available_license_capacity} available out of {report.total_license_capacity} total."
        )

        try:
            if odoo_client.model_exists("cs.m365.tenant"):
                # -- Primary path: cs.m365.tenant (self-hosted Odoo with addon) --
                tenant_vals = {
                    "name": report.display_name or "Microsoft 365 Demo Tenant",
                    "tenant_id": report.tenant_id,
                    "primary_domain": report.primary_domain,
                    "readiness_status": report.readiness_status,
                    "total_license_capacity": report.total_license_capacity,
                    "consumed_license_capacity": report.consumed_license_capacity,
                    "available_license_capacity": report.available_license_capacity,
                    "discovery_errors": json.dumps(report.discovery_errors, ensure_ascii=False) if report.discovery_errors else False,
                    "last_error": report.last_error or False,
                }
                tenant_vals = supported_vals("cs.m365.tenant", tenant_vals)
                existing = odoo_client.search_read(
                    "cs.m365.tenant",
                    [["tenant_id", "=", report.tenant_id]],
                    fields=["id"],
                    limit=1,
                )
                if existing:
                    odoo_client.write("cs.m365.tenant", [existing[0]["id"]], tenant_vals)
                    rec_id = existing[0]["id"]
                else:
                    rec_id = odoo_client.create_one("cs.m365.tenant", tenant_vals)

                # Child: Domains
                existing_domains = {
                    r["domain_id"]: r["id"]
                    for r in odoo_client.search_read(
                        "cs.m365.domain",
                        [["tenant_record_id", "=", rec_id]],
                        fields=["id", "domain_id"],
                    )
                }
                for d in report.domains:
                    did = d.get("domain_id") or d.get("name")
                    if not did:
                        continue
                    dvals = {
                        "tenant_record_id": rec_id,
                        "domain_id": did,
                        "name": d.get("name", did),
                        "is_default": d.get("is_default", False),
                        "is_initial": d.get("is_initial", False),
                        "status": d.get("status", "Verified"),
                    }
                    if did in existing_domains:
                        odoo_client.write("cs.m365.domain", [existing_domains[did]], dvals)
                    else:
                        odoo_client.create_one("cs.m365.domain", dvals)

                # Child: SKUs
                existing_skus = {
                    r["sku_id"]: r["id"]
                    for r in odoo_client.search_read(
                        "cs.m365.sku",
                        [["tenant_record_id", "=", rec_id]],
                        fields=["id", "sku_id"],
                    )
                }
                for s in report.skus:
                    sid = s.get("sku_id")
                    if not sid:
                        continue
                    svals = {
                        "tenant_record_id": rec_id,
                        "sku_id": sid,
                        "sku_part_number": s.get("sku_part_number", ""),
                        "capability_status": s.get("capability_status", "Enabled"),
                        "enabled_units": s.get("enabled_units", 0),
                        "consumed_units": s.get("consumed_units", 0),
                        "available_units": s.get("available_units", 0),
                    }
                    if sid in existing_skus:
                        odoo_client.write("cs.m365.sku", [existing_skus[sid]], svals)
                    else:
                        odoo_client.create_one("cs.m365.sku", svals)

                # Child: LAB Groups
                existing_groups = {
                    r["graph_id"]: r["id"]
                    for r in odoo_client.search_read(
                        "cs.m365.group",
                        [["tenant_record_id", "=", rec_id]],
                        fields=["id", "graph_id"],
                    )
                }
                for g in report.lab_groups:
                    gid = g.get("graph_id") or g.get("id")
                    if not gid:
                        continue
                    gvals = {
                        "tenant_record_id": rec_id,
                        "graph_id": gid,
                        "name": g.get("name", ""),
                        "description": g.get("description", ""),
                        "group_type": g.get("group_type", "Security"),
                        "mail_nickname": g.get("mail_nickname", ""),
                        "is_lab_group": g.get("is_lab_group", True),
                    }
                    if gid in existing_groups:
                        odoo_client.write("cs.m365.group", [existing_groups[gid]], gvals)
                    else:
                        odoo_client.create_one("cs.m365.group", gvals)

                # Child: Capabilities
                existing_caps = {
                    r["capability_key"]: r["id"]
                    for r in odoo_client.search_read(
                        "cs.m365.capability",
                        [["tenant_record_id", "=", rec_id]],
                        fields=["id", "capability_key"],
                    )
                }
                for c in report.capabilities:
                    ckey = c.get("capability_key")
                    if not ckey:
                        continue
                    cvals = {
                        "tenant_record_id": rec_id,
                        "capability_key": ckey,
                        "name": c.get("name", ckey),
                        "status": c.get("status", "available"),
                        "detail": c.get("detail", ""),
                    }
                    if ckey in existing_caps:
                        odoo_client.write("cs.m365.capability", [existing_caps[ckey]], cvals)
                    else:
                        odoo_client.create_one("cs.m365.capability", cvals)

                model_used = "cs.m365.tenant"

            elif odoo_client.model_exists("x_m365_tenant"):
                # -- Dedicated Studio model: x_m365_tenant (Odoo Online) --
                last_check_str = report.last_readiness_check.replace("T", " ")[:19] if report.last_readiness_check else False
                t_vals = {
                    "x_name": report.display_name or "Microsoft 365 Tenant",
                    "x_tenant_id": report.tenant_id,
                    "x_primary_domain": report.primary_domain,
                    "x_domains": json.dumps(report.domains, ensure_ascii=False),
                    "x_subscribed_skus": json.dumps(report.skus, ensure_ascii=False),
                    "x_total_licenses": report.total_license_capacity,
                    "x_consumed_licenses": report.consumed_license_capacity,
                    "x_available_licenses": report.available_license_capacity,
                    "x_lab_groups": json.dumps(report.lab_groups, ensure_ascii=False),
                    "x_graph_capabilities": json.dumps(report.capabilities, ensure_ascii=False),
                    "x_discovery_errors": json.dumps(report.discovery_errors, ensure_ascii=False),
                    "x_readiness_status": report.readiness_status,
                    "x_last_readiness_check": last_check_str,
                    "x_last_error": report.last_error or False,
                }
                t_vals = supported_vals("x_m365_tenant", t_vals)
                existing = odoo_client.search_read(
                    "x_m365_tenant",
                    [["x_tenant_id", "=", report.tenant_id]],
                    fields=["id"],
                    limit=1,
                )
                if existing:
                    odoo_client.write("x_m365_tenant", [existing[0]["id"]], t_vals)
                    rec_id = existing[0]["id"]
                else:
                    rec_id = odoo_client.create_one("x_m365_tenant", t_vals)
                model_used = "x_m365_tenant"

            elif odoo_client.model_exists("x_integration_config"):
                # -- Fallback: x_integration_config (Odoo Online without addon) --
                last_check_str = report.last_readiness_check.replace("T", " ")[:19] if report.last_readiness_check else False
                state_val = "done" if report.readiness_status in ("ready", "partial") else "failed"
                vals = {
                    "x_name": "Microsoft 365 Tenant Readiness",
                    "x_provider": "m365_graph",
                    "x_sync_state": state_val,
                    "x_last_sync_at": last_check_str,
                    "x_notes": notes_json,
                    "x_sync_message": summary_msg,
                    "x_active": True,
                }
                existing = odoo_client.search_read(
                    "x_integration_config",
                    [["x_name", "=", "Microsoft 365 Tenant Readiness"]],
                    fields=["id"],
                    limit=1,
                )
                if existing:
                    odoo_client.write("x_integration_config", [existing[0]["id"]], vals)
                    rec_id = existing[0]["id"]
                else:
                    rec_id = odoo_client.create_one("x_integration_config", vals)
                model_used = "x_integration_config"
            else:
                return {"status": "skipped", "reason": "No compatible Odoo model accessible"}

            LOGGER.info("Persisted Tenant Readiness to Odoo Online (%s id=%s)", model_used, rec_id)
            return {
                "status": "success",
                "model": model_used,
                "record_id": rec_id,
            }

        except Exception as exc:
            sanitized_err = sanitize(exc)
            LOGGER.warning("Could not persist Tenant Readiness to Odoo Online: %s", sanitized_err)
            return {"status": "fallback", "reason": sanitized_err}


__all__ = ["TenantReadinessService", "TenantReadinessReport"]
