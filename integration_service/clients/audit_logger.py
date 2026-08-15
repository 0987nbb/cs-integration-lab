# -*- coding: utf-8 -*-
"""Structured audit logging representation for Microsoft Graph API requests."""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..sanitize import sanitize

LOGGER = logging.getLogger("integration_service.ms_graph.audit")

_VALID_HTTP_METHODS = {"GET", "POST", "PATCH", "PUT", "DELETE"}


@dataclass
class GraphAuditEvent:
    """Structured audit event for a Microsoft Graph interaction."""

    operation_name: str
    http_method: str
    resource_path: str
    tenant_id: str = ""
    request_id: Optional[str] = None
    correlation_id: Optional[str] = None
    http_status: Optional[int] = None
    success: bool = True
    retry_count: int = 0
    sanitized_error: str = ""
    duration_ms: float = 0.0
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["sanitized_error"] = sanitize(self.sanitized_error)
        return data


class GraphAuditLogger:
    """In-memory structured audit logger for Graph events."""

    def __init__(self) -> None:
        self.events: List[GraphAuditEvent] = []

    def record(self, event: GraphAuditEvent) -> None:
        event.sanitized_error = sanitize(event.sanitized_error)
        self.events.append(event)
        LOGGER.info(
            "Graph Audit [%s]: %s %s status=%s request_id=%s success=%s duration=%.2fms",
            event.tenant_id or "default",
            event.http_method,
            event.resource_path,
            event.http_status,
            event.request_id or "N/A",
            event.success,
            event.duration_ms,
        )

    def clear(self) -> None:
        self.events.clear()

    def sync_to_odoo(
        self,
        odoo_client: Any,
        operation_label: str = "",
        fallback_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Persist all in-memory audit events to Odoo or a JSONL fallback file.

        Primary: cs.m365.graph.audit.log (self-hosted Odoo with addon installed)
        Fallback: local JSONL file (for Odoo Online / no addon)

        Never stores bearer tokens, access tokens, or client secrets.
        """
        if not self.events:
            return {"status": "skipped", "reason": "No audit events to persist"}

        written = 0
        errors = 0

        try:
            def supported_vals(model: str, vals: Dict[str, Any]) -> Dict[str, Any]:
                try:
                    fields_meta = odoo_client.fields_get(model)
                    return {k: v for k, v in vals.items() if k in fields_meta}
                except Exception:
                    return vals

            if odoo_client and odoo_client.model_exists("cs.m365.graph.audit.log"):
                # -- Primary path: cs.m365.graph.audit.log --
                for event in self.events:
                    ts = event.timestamp
                    if ts and "T" in ts:
                        ts_odoo = ts.replace("T", " ")[:19]
                    else:
                        ts_odoo = ts or False

                    method = event.http_method.upper() if event.http_method else "GET"
                    if method not in _VALID_HTTP_METHODS:
                        method = "GET"

                    entry_name = f"{method} {event.resource_path}"[:255]

                    log_vals = {
                        "name": entry_name,
                        "operation_label": operation_label or event.operation_name,
                        "tenant_display": event.tenant_id or "",
                        "resource": event.resource_path,
                        "http_method": method,
                        "graph_request_id": event.request_id or "",
                        "correlation_id": event.correlation_id or "",
                        "timestamp": ts_odoo,
                        "http_status": event.http_status or 0,
                        "success": event.success,
                        "sanitized_error": sanitize(event.sanitized_error) or False,
                        "retry_count": event.retry_count,
                        "duration_ms": event.duration_ms,
                    }
                    log_vals = supported_vals("cs.m365.graph.audit.log", log_vals)
                    try:
                        odoo_client.create_one("cs.m365.graph.audit.log", log_vals)
                        written += 1
                    except Exception as exc:
                        LOGGER.warning(
                            "Failed to persist Graph audit event %s: %s",
                            entry_name, sanitize(exc),
                        )
                        errors += 1

                LOGGER.info(
                    "Graph Audit sync: %s events written to cs.m365.graph.audit.log (%s errors)",
                    written, errors,
                )
                return {
                    "status": "success",
                    "model": "cs.m365.graph.audit.log",
                    "written": written,
                    "errors": errors,
                }

            elif odoo_client and odoo_client.model_exists("x_m365_graph_audit_log"):
                # -- Dedicated Studio model: x_m365_graph_audit_log (Odoo Online) --
                for event in self.events:
                    ts = event.timestamp
                    if ts and "T" in ts:
                        ts_odoo = ts.replace("T", " ")[:19]
                    else:
                        ts_odoo = ts or False

                    method = event.http_method.upper() if event.http_method else "GET"
                    if method not in _VALID_HTTP_METHODS:
                        method = "GET"

                    entry_name = f"{method} {event.resource_path}"[:255]

                    log_vals = {
                        "x_name": entry_name,
                        "x_operation_label": operation_label or event.operation_name,
                        "x_tenant_display": event.tenant_id or "",
                        "x_resource": event.resource_path,
                        "x_http_method": method,
                        "x_graph_request_id": event.request_id or "",
                        "x_correlation_id": event.correlation_id or "",
                        "x_timestamp": ts_odoo,
                        "x_http_status": event.http_status or 0,
                        "x_success": event.success,
                        "x_sanitized_error": sanitize(event.sanitized_error) or False,
                        "x_retry_count": event.retry_count,
                        "x_duration_ms": event.duration_ms,
                    }
                    log_vals = supported_vals("x_m365_graph_audit_log", log_vals)
                    try:
                        odoo_client.create_one("x_m365_graph_audit_log", log_vals)
                        written += 1
                    except Exception as exc:
                        LOGGER.warning(
                            "Failed to persist Graph audit event %s: %s",
                            entry_name, sanitize(exc),
                        )
                        errors += 1

                LOGGER.info(
                    "Graph Audit sync: %s events written to x_m365_graph_audit_log (%s errors)",
                    written, errors,
                )
                return {
                    "status": "success",
                    "model": "x_m365_graph_audit_log",
                    "written": written,
                    "errors": errors,
                }

            else:
                # -- Fallback: JSONL file --
                fp = fallback_path or os.path.join("logs", "graph_audit.jsonl")
                os.makedirs(os.path.dirname(os.path.abspath(fp)), exist_ok=True)
                with open(fp, "a", encoding="utf-8") as fh:
                    for event in self.events:
                        fh.write(
                            json.dumps(event.to_dict(), ensure_ascii=False, default=str)
                            + "\n"
                        )
                        written += 1
                LOGGER.info(
                    "Graph Audit fallback: %s events written to %s", written, fp
                )
                return {
                    "status": "fallback",
                    "path": fp,
                    "written": written,
                }

        except Exception as exc:
            sanitized_err = sanitize(exc)
            LOGGER.warning("Could not persist Graph audit events: %s", sanitized_err)
            return {"status": "error", "reason": sanitized_err}


__all__ = ["GraphAuditEvent", "GraphAuditLogger"]

