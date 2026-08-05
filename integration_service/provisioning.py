# -*- coding: utf-8 -*-
"""Idempotent schema provisioning on the target Odoo instance.

The target is an Odoo Online trial, where no addon can be installed, so the
custom fields this service relies on are created through the API as *manual*
``ir.model.fields`` records - the same mechanism that produced the existing
``x_external_id`` / ``x_source_hash`` / ``x_external_updated_at`` fields.

Only fields on **existing** models are provisioned here. New manual *models*
are deliberately not created: a manual model has no access rule, and
``ir.model.access`` is not reachable over the JSON-2 route, so such a model
would be unreadable by this very service (verified: HTTP 403). The 7-day
forecast therefore lives in columns on ``res.partner``, which already has
working access rules.

Running this twice is a no-op; every field is checked before it is created.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .errors import OdooError
from .sanitize import sanitize

LOGGER = logging.getLogger("integration_service.provisioning")

#: Custom fields added to ``res.partner`` for the Open-Meteo forecast.
PARTNER_FORECAST_FIELDS: List[Dict[str, Any]] = [
    {
        "name": "x_forecast_updated_at",
        "ttype": "datetime",
        "field_description": "Forecast Updated At",
        "help": "When the Open-Meteo forecast was last refreshed for this contact.",
    },
    {
        "name": "x_forecast_payload",
        "ttype": "text",
        "field_description": "Forecast (7 days, JSON)",
        "help": "Full 7-day forecast as JSON: [{date, temp_max, temp_min}, ...].",
    },
    {
        "name": "x_forecast_next_date",
        "ttype": "date",
        "field_description": "Next-Day Forecast Date",
        "help": "Date of the next-day forecast shown on this contact.",
    },
    {
        "name": "x_forecast_next_temp_max",
        "ttype": "float",
        "field_description": "Next-Day Max Temp (C)",
        "help": "Next-day maximum temperature in degrees Celsius.",
    },
    {
        "name": "x_forecast_next_temp_min",
        "ttype": "float",
        "field_description": "Next-Day Min Temp (C)",
        "help": "Next-day minimum temperature in degrees Celsius.",
    },
    {
        "name": "x_forecast_next_summary",
        "ttype": "char",
        "field_description": "Next-Day Forecast",
        "help": "Human-readable next-day forecast, e.g. '2026-08-06: 33.7 C / 25.2 C'.",
    },
]

#: Fields the idempotency layer depends on, checked but never created here
#: because they already exist on the target instance.
IDEMPOTENCY_FIELDS = ("x_external_id", "x_source_hash", "x_external_updated_at")

IDEMPOTENT_MODELS = ("res.partner", "project.task", "helpdesk.ticket", "calendar.event")


def _model_id(client: Any, model: str) -> Optional[int]:
    rows = client.search_read("ir.model", [["model", "=", model]], fields=["id"], limit=1)
    return rows[0]["id"] if rows else None


def existing_field_names(client: Any, model: str, prefix: str = "x_") -> set:
    rows = client.search_read_all(
        "ir.model.fields",
        [["model", "=", model], ["name", "like", prefix]],
        fields=["name"],
    )
    return {row["name"] for row in rows}


def ensure_partner_forecast_fields(client: Any, dry_run: bool = False) -> Dict[str, Any]:
    """Create any missing forecast column on ``res.partner``.

    Returns a report ``{"created": [...], "existing": [...], "failed": {...}}``.
    """
    report: Dict[str, Any] = {"created": [], "existing": [], "failed": {}}
    model = "res.partner"

    model_id = _model_id(client, model)
    if not model_id:
        report["failed"]["*"] = f"Could not resolve ir.model id for {model}."
        return report

    present = existing_field_names(client, model)

    for spec in PARTNER_FORECAST_FIELDS:
        name = spec["name"]
        if name in present:
            report["existing"].append(name)
            continue
        if dry_run:
            LOGGER.info("[MOCK WRITE] would create %s.%s (%s)", model, name, spec["ttype"])
            report["created"].append(name)
            continue
        vals = {
            "name": name,
            "model": model,
            "model_id": model_id,
            "ttype": spec["ttype"],
            "field_description": spec["field_description"],
            "help": spec.get("help", ""),
            "state": "manual",
            "store": True,
        }
        try:
            client.create_one("ir.model.fields", vals)
            report["created"].append(name)
            LOGGER.info("Created custom field %s.%s (%s)", model, name, spec["ttype"])
        except OdooError as exc:
            report["failed"][name] = sanitize(exc)
            LOGGER.error("Could not create %s.%s: %s", model, name, sanitize(exc))

    return report


def verify_idempotency_fields(client: Any) -> Dict[str, List[str]]:
    """Report which models are missing an idempotency field."""
    missing: Dict[str, List[str]] = {}
    for model in IDEMPOTENT_MODELS:
        try:
            present = existing_field_names(client, model)
        except OdooError as exc:
            missing[model] = [f"unreadable: {sanitize(exc)}"]
            continue
        gaps = [f for f in IDEMPOTENCY_FIELDS if f not in present]
        if gaps:
            missing[model] = gaps
    return missing


def check_access(client: Any, models: Optional[List[str]] = None) -> Dict[str, str]:
    """Probe read access for each model, returning ``{model: "ok" | reason}``."""
    targets = models or [
        "res.partner", "project.task", "helpdesk.ticket", "calendar.event",
        "res.currency", "res.currency.rate", "mail.activity",
        "x_integration_config", "x_integration_sync_log",
    ]
    out: Dict[str, str] = {}
    for model in targets:
        try:
            client.search_count(model, [])
            out[model] = "ok"
        except OdooError as exc:
            out[model] = "no access rule (HTTP 403)" if exc.is_access_error else sanitize(exc)[:160]
    return out


def provision(client: Any, dry_run: bool = False) -> Dict[str, Any]:
    """Run every provisioning step and return a combined report."""
    return {
        "partner_forecast_fields": ensure_partner_forecast_fields(client, dry_run=dry_run),
        "missing_idempotency_fields": verify_idempotency_fields(client),
        "model_access": check_access(client),
    }


__all__ = [
    "PARTNER_FORECAST_FIELDS",
    "check_access",
    "ensure_partner_forecast_fields",
    "provision",
    "verify_idempotency_fields",
]
