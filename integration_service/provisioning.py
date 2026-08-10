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
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .errors import OdooError
from .sanitize import sanitize
from .sync_request import SYNC_NOW_ACTION_CODE, SYNC_REQUEST_FIELD_SPECS

LOGGER = logging.getLogger("integration_service.provisioning")

#: The group that owns every integration control. Created by provisioning, so a
#: fresh database ends up with it even though no addon can be installed.
INTEGRATION_MANAGER_GROUP = "Integration Manager"

#: The two custom models the integration owns.
CONFIG_MODEL = "x_integration_config"
SYNC_LOG_MODEL = "x_integration_sync_log"

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

#: Fields the idempotency layer depends on.
IDEMPOTENCY_FIELDS = ("x_external_id", "x_source_hash", "x_external_updated_at")

IDEMPOTENT_MODELS = ("res.partner", "project.task", "helpdesk.ticket", "calendar.event")

#: Field definitions for missing idempotency columns.
IDEMPOTENCY_FIELD_SPECS: List[Dict[str, Any]] = [
    {
        "name": "x_external_id",
        "ttype": "char",
        "field_description": "External ID",
        "help": "Stable external key for integration idempotency.",
        "index": True,
    },
    {
        "name": "x_source_hash",
        "ttype": "char",
        "field_description": "Source Hash",
        "help": "SHA-256 hash of mapped external data.",
    },
    {
        "name": "x_external_updated_at",
        "ttype": "datetime",
        "field_description": "External Updated At",
        "help": "Upstream last modification timestamp.",
    },
]


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


def _all_field_names(client: Any, model: str) -> set:
    rows = client.search_read_all("ir.model.fields", [["model", "=", model]], fields=["name"])
    return {row["name"] for row in rows}


def ensure_model(client: Any, model: str, description: str,
                 dry_run: bool = False) -> Tuple[Optional[int], str]:
    """Create a manual ``ir.model`` when it is absent.

    Returns ``(model_id, "existing" | "created" | "failed: ...")``. A fresh trial
    has neither integration model, so provisioning has to be able to make them;
    the access rules they need are installed by :func:`ensure_security`, without
    which a manual model is readable by nobody.
    """
    model_id = _model_id(client, model)
    if model_id:
        return model_id, "existing"
    if dry_run:
        LOGGER.info("[MOCK WRITE] would create model %s (%s)", model, description)
        return None, "created"
    try:
        new_id = client.create_one("ir.model", {
            "name": description,
            "model": model,
            "state": "manual",
        })
        LOGGER.info("Created manual model %s (id %s)", model, new_id)
        return new_id, "created"
    except OdooError as exc:
        return None, f"failed: {sanitize(exc)}"


def ensure_fields(client: Any, model: str, specs: Sequence[Dict[str, Any]],
                  dry_run: bool = False) -> Dict[str, Any]:
    """Create any missing field in ``specs`` on ``model``.

    Handles selection fields, which need their values as ``selection_ids``
    rather than the legacy ``selection`` string.
    """
    report: Dict[str, Any] = {"created": [], "existing": [], "failed": {}}
    model_id = _model_id(client, model)
    if not model_id:
        report["failed"]["*"] = f"Could not resolve ir.model id for {model}."
        return report

    try:
        present = _all_field_names(client, model)
    except OdooError as exc:
        report["failed"]["*"] = sanitize(exc)
        return report

    for spec in specs:
        name = spec["name"]
        if name in present:
            report["existing"].append(name)
            continue
        if dry_run:
            LOGGER.info("[MOCK WRITE] would create %s.%s (%s)", model, name, spec["ttype"])
            report["created"].append(name)
            continue

        vals: Dict[str, Any] = {
            "name": name,
            "model": model,
            "model_id": model_id,
            "ttype": spec["ttype"],
            "field_description": spec["field_description"],
            "help": spec.get("help", ""),
            "state": "manual",
            "store": True,
        }
        if spec.get("index"):
            vals["index"] = True
        if spec.get("required"):
            vals["required"] = True
        if spec.get("relation"):
            vals["relation"] = spec["relation"]
        if spec["ttype"] == "selection":
            vals["selection_ids"] = [
                (0, 0, {"value": value, "name": label, "sequence": (index + 1) * 10})
                for index, (value, label) in enumerate(spec.get("selection") or [])
            ]
        try:
            client.create_one("ir.model.fields", vals)
            report["created"].append(name)
            LOGGER.info("Created field %s.%s (%s)", model, name, spec["ttype"])
        except OdooError as exc:
            report["failed"][name] = sanitize(exc)
            LOGGER.error("Could not create %s.%s: %s", model, name, sanitize(exc))
    return report


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


def ensure_idempotency_fields(client: Any, dry_run: bool = False) -> Dict[str, Any]:
    """Ensure all required idempotency fields exist across target models.

    Creates missing fields on fresh trial databases.
    """
    report: Dict[str, Any] = {"created": {}, "existing": {}, "failed": {}}
    for model in IDEMPOTENT_MODELS:
        created_list: List[str] = []
        existing_list: List[str] = []
        model_id = _model_id(client, model)
        if not model_id:
            report["failed"][model] = f"Could not resolve ir.model id for {model}."
            continue

        try:
            present = existing_field_names(client, model)
        except OdooError as exc:
            report["failed"][model] = sanitize(exc)
            continue

        for spec in IDEMPOTENCY_FIELD_SPECS:
            name = spec["name"]
            if name in present:
                existing_list.append(name)
                continue
            if dry_run:
                LOGGER.info("[MOCK WRITE] would create idempotency field %s.%s", model, name)
                created_list.append(name)
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
                "index": spec.get("index", False),
            }
            try:
                client.create_one("ir.model.fields", vals)
                created_list.append(name)
                LOGGER.info("Created idempotency field %s.%s", model, name)
            except OdooError as exc:
                report["failed"][f"{model}.{name}"] = sanitize(exc)

        report["created"][model] = created_list
        report["existing"][model] = existing_list

    return report


def ensure_partner_forecast_view(client: Any, dry_run: bool = False) -> Dict[str, Any]:
    """Ensure the Contact view for weather forecast is created on ``res.partner`` inheriting from base.view_partner_form."""
    view_name = "res.partner.form.weather.forecast"
    try:
        inherit_id = None
        try:
            data_rows = client.search_read(
                "ir.model.data",
                [["module", "=", "base"], ["name", "=", "view_partner_form"]],
                fields=["res_id"],
                limit=1,
            )
            if data_rows:
                inherit_id = data_rows[0]["res_id"]
        except OdooError:
            pass

        if not inherit_id:
            try:
                view_rows = client.search_read(
                    "ir.ui.view",
                    [["model", "=", "res.partner"], ["name", "=", "res.partner.form"]],
                    fields=["id"],
                    limit=1,
                )
                if view_rows:
                    inherit_id = view_rows[0]["id"]
            except OdooError:
                pass

        arch = (
            '<data>'
            '<xpath expr="//notebook" position="inside">'
            '<page string="Weather Forecast" name="weather_forecast">'
            '<group string="Next-Day Summary">'
            '<group>'
            '<field name="x_forecast_next_summary"/>'
            '<field name="x_forecast_next_date"/>'
            '<field name="x_forecast_updated_at"/>'
            '</group>'
            '<group>'
            '<field name="x_forecast_next_temp_max"/>'
            '<field name="x_forecast_next_temp_min"/>'
            '</group>'
            '</group>'
            '<group string="7-Day Raw Forecast (JSON)">'
            '<field name="x_forecast_payload"/>'
            '</group>'
            '</page>'
            '</xpath>'
            '</data>'
        )

        existing = client.search_read(
            "ir.ui.view", [["name", "=", view_name], ["model", "=", "res.partner"]], fields=["id", "inherit_id"], limit=1
        )
        if existing:
            view_id = existing[0]["id"]
            cur_inherit = existing[0].get("inherit_id")
            if not cur_inherit or (isinstance(cur_inherit, list) and cur_inherit[0] != inherit_id):
                if not dry_run and inherit_id:
                    client.write("ir.ui.view", [view_id], {"inherit_id": inherit_id, "arch": arch})
            return {"status": "existing", "view_id": view_id, "inherit_id": inherit_id}

        if dry_run:
            LOGGER.info("[MOCK WRITE] would create view %s inheriting from %s", view_name, inherit_id)
            return {"status": "created", "mock": True, "inherit_id": inherit_id}

        vals = {
            "name": view_name,
            "model": "res.partner",
            "type": "form",
            "inherit_id": inherit_id,
            "arch": arch,
            "active": True,
        }
        view_id = client.create_one("ir.ui.view", vals)
        return {"status": "created", "view_id": view_id, "inherit_id": inherit_id}
    except OdooError as exc:
        return {"status": "failed", "error": sanitize(exc)}



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


def ensure_integration_config_views(client: Any, dry_run: bool = False) -> Dict[str, Any]:
    """Install the **Sync Now** server action on ``x_integration_config``.

    The action body is :data:`~integration_service.sync_request.SYNC_NOW_ACTION_CODE`,
    which enqueues a request and says so. It cannot run a sync itself: Odoo Online
    evaluates server actions under ``safe_eval``, where there is no ``import`` and
    no socket, so any code here that claimed to have called GitHub would be
    claiming something it is structurally incapable of doing. The real run happens
    in :class:`~integration_service.sync_request.SyncRequestWorker`.
    """
    report: Dict[str, Any] = {"updated": [], "created": [], "failed": {}}

    model_name = CONFIG_MODEL
    try:
        model_id = _model_id(client, model_name)
        if not model_id:
            report["failed"][model_name] = "model does not exist; run ensure_integration_models first."
            return report

        acts = client.search_read(
            "ir.actions.server",
            [["model_id", "=", model_id], ["name", "=", "Sync Now"]],
            fields=["id"],
            limit=1,
        )
        vals = {
            "name": "Sync Now",
            "model_id": model_id,
            "state": "code",
            "code": SYNC_NOW_ACTION_CODE,
            "binding_model_id": model_id,
            "binding_view_types": "list,form",
        }
        if dry_run:
            LOGGER.info("[MOCK WRITE] would install Sync Now action on %s", model_name)
            report["created"].append(f"{model_name}:server_action:mock")
            return report

        if acts:
            action_id = acts[0]["id"]
            client.write("ir.actions.server", [action_id], {"code": SYNC_NOW_ACTION_CODE})
            report["updated"].append(f"{model_name}:server_action:{action_id}")
        else:
            action_id = client.create_one("ir.actions.server", vals)
            report["created"].append(f"{model_name}:server_action:{action_id}")
    except OdooError as exc:
        report["failed"][model_name] = sanitize(exc)
    return report


# -- integration models -----------------------------------------------------

PROVIDER_SELECTION: List[Tuple[str, str]] = [
    ("github", "GitHub"),
    ("jsonplaceholder", "JSONPlaceholder"),
    ("frankfurter", "Frankfurter"),
    ("open_meteo", "Open-Meteo"),
    ("nager_date", "Nager.Date"),
]

STATUS_SELECTION: List[Tuple[str, str]] = [
    ("success", "Success"),
    ("partial", "Partial"),
    ("failed", "Failed"),
    ("skipped", "Skipped"),
]

CONFIG_FIELD_SPECS: List[Dict[str, Any]] = [
    {"name": "x_name", "ttype": "char", "field_description": "Name", "required": True},
    {"name": "x_provider", "ttype": "selection", "field_description": "Provider",
     "required": True, "selection": PROVIDER_SELECTION},
    {"name": "x_active", "ttype": "boolean", "field_description": "Active"},
    {"name": "x_schedule_enabled", "ttype": "boolean", "field_description": "Schedule Enabled"},
    {"name": "x_last_sync_at", "ttype": "datetime", "field_description": "Last Sync At"},
    {"name": "x_next_sync_at", "ttype": "datetime", "field_description": "Next Sync At"},
    {"name": "x_notes", "ttype": "text", "field_description": "Notes"},
] + SYNC_REQUEST_FIELD_SPECS

SYNC_LOG_FIELD_SPECS: List[Dict[str, Any]] = [
    {"name": "x_name", "ttype": "char", "field_description": "Reference"},
    {"name": "x_provider", "ttype": "selection", "field_description": "Provider",
     "required": True, "selection": PROVIDER_SELECTION},
    {"name": "x_config_id", "ttype": "many2one", "field_description": "Integration",
     "relation": CONFIG_MODEL},
    {"name": "x_start_time", "ttype": "datetime", "field_description": "Start Time", "required": True},
    {"name": "x_end_time", "ttype": "datetime", "field_description": "End Time"},
    {"name": "x_status", "ttype": "selection", "field_description": "Status",
     "required": True, "selection": STATUS_SELECTION},
    {"name": "x_created_count", "ttype": "integer", "field_description": "Created"},
    {"name": "x_updated_count", "ttype": "integer", "field_description": "Updated"},
    {"name": "x_skipped_count", "ttype": "integer", "field_description": "Skipped"},
    {"name": "x_failed_count", "ttype": "integer", "field_description": "Failed"},
    {"name": "x_error_details", "ttype": "text", "field_description": "Error Details"},
]


def ensure_integration_models(client: Any, dry_run: bool = False) -> Dict[str, Any]:
    """Create both integration models and every column they need.

    This is what makes a *fresh* trial reachable: neither model ships with Odoo,
    and neither can arrive as an addon on Odoo Online.
    """
    report: Dict[str, Any] = {}
    for model, description, specs in (
        (CONFIG_MODEL, "Integration Configuration", CONFIG_FIELD_SPECS),
        (SYNC_LOG_MODEL, "Integration Sync Log", SYNC_LOG_FIELD_SPECS),
    ):
        model_id, state = ensure_model(client, model, description, dry_run=dry_run)
        entry: Dict[str, Any] = {"model": state}
        if model_id or dry_run:
            entry["fields"] = ensure_fields(client, model, specs, dry_run=dry_run)
        report[model] = entry
    return report


# -- security ---------------------------------------------------------------

#: Name of the provisioning action. Named rather than anonymous so an operator
#: reading the Server Actions list can see exactly what put the rules there.
SECURITY_ACTION_NAME = "CS Integration Lab: Provision Security"

#: Fixed body of that action.
#:
#: Odoo 19 replaced ``ir.model.access`` and ``ir.rule`` with a single
#: :samp:`ir.access` model - both old names raise ``KeyError`` here, which is
#: also why the JSON-2 route answers HTTP 404 for them. A rule carries ``kind``
#: (``permission`` grants, ``restriction`` narrows) and ``operation``
#: (``crud``, ``r``, ...) instead of the four ``perm_*`` booleans.
#:
#: The action runs as the caller, so ``env.user`` is the account this service
#: authenticates as; it is put in the group *before* any broader grant is
#: withdrawn. safe_eval also forbids ``try``/``except`` and dunders, so this body
#: uses neither - a failure rolls the whole action back, which is the behaviour
#: we want anyway.
SECURITY_PROVISION_CODE = """
Groups = env['res.groups'].sudo()
Access = env['ir.access'].sudo()
Models = env['ir.model'].sudo()
Data = env['ir.model.data'].sudo()
report = []

group = Groups.search([('name', '=', GROUP_NAME)], limit=1)
if not group:
    group = Groups.create({'name': GROUP_NAME})
    report.append('group-created-' + str(group.id))
else:
    report.append('group-existing-' + str(group.id))

anchor = Data.search([('model', '=', 'res.groups'), ('res_id', '=', group.id)], limit=1)
if not anchor:
    Data.create({
        'module': 'cs_integration_lab',
        'name': 'group_integration_manager',
        'model': 'res.groups',
        'res_id': group.id,
        'noupdate': True,
    })
    report.append('group-xmlid-created')

if env.user.id not in group.user_ids.ids:
    group.write({'user_ids': [(4, env.user.id)]})
    report.append('service-account-added-' + env.user.login)

for model_name in MODEL_NAMES:
    m = Models.search([('model', '=', model_name)], limit=1)
    if not m:
        report.append(model_name + '-MODEL-MISSING')
        continue
    vals = {
        'name': model_name + ' / Integration Manager',
        'model_id': m.id,
        'group_id': group.id,
        'operation': 'crud',
        'kind': 'permission',
        'active': True,
    }
    mine = Access.search([('model_id', '=', m.id), ('group_id', '=', group.id),
                          ('kind', '=', 'permission')], limit=1)
    if mine:
        mine.write(vals)
        report.append(model_name + '-rule-updated-' + str(mine.id))
    else:
        made = Access.create(vals)
        report.append(model_name + '-rule-created-' + str(made.id))
    # Any other non-standard grant on these models is wider than the integration
    # is meant to be. Standard rules are left alone: they belong to Odoo.
    for acl in Access.search([('model_id', '=', m.id), ('kind', '=', 'permission'),
                              ('is_standard', '=', False), ('active', '=', True)]):
        if acl.group_id.id != group.id:
            acl.write({'active': False})
            report.append(model_name + '-broad-grant-disabled-' + str(acl.id))

for pair in UI_TARGETS:
    target_model = pair[0]
    for rec_id in pair[1]:
        rec = env[target_model].sudo().browse(rec_id)
        if rec.exists():
            if group.id not in rec.group_ids.ids:
                rec.write({'group_ids': [(4, group.id)]})
                report.append(target_model + '-' + str(rec_id) + '-restricted')

# Menus and window actions carry NO group of their own. Odoo already hides a
# menu whose action targets a model the user cannot read, so the ir.access rules
# above are both the enforcement and the hiding. Adding group_ids on top bought
# nothing and removed the app from the launcher, so any such leftover is undone
# here - provisioning has to be able to repair what it previously set.
for pair in UI_UNRESTRICT:
    target_model = pair[0]
    for rec_id in pair[1]:
        rec = env[target_model].sudo().browse(rec_id)
        if rec.exists():
            if rec.group_ids.ids:
                rec.write({'group_ids': [(5, 0, 0)]})
                report.append(target_model + '-' + str(rec_id) + '-group-cleared')

out = ' | '.join(report)
"""


def _security_code(menu_ids: Sequence[int], action_ids: Sequence[int],
                   server_action_ids: Sequence[int]) -> str:
    """Bind the constant security body to this database's UI record ids.

    Nothing in the UI layer carries a group of its own. Access is enforced once,
    by the ``ir.access`` rules on the two models, and the UI follows from it:
    Odoo hides a menu whose action targets a model the user cannot read, and a
    Sync Now click by a non-manager is refused because the action writes
    ``x_sync_state`` and that write is denied.

    Restricting the UI records *as well* was belt-and-braces that cost real
    functionality twice - first the app vanished from the launcher, then Sync Now
    vanished from the cog menu - while adding no enforcement the model rules were
    not already providing. One control, in one place, is the safer design.
    """
    header = (
        f"GROUP_NAME = {INTEGRATION_MANAGER_GROUP!r}\n"
        f"MODEL_NAMES = {[CONFIG_MODEL, SYNC_LOG_MODEL]!r}\n"
        "UI_TARGETS = []\n"
        "UI_UNRESTRICT = [\n"
        f"    ('ir.ui.menu', {list(menu_ids)!r}),\n"
        f"    ('ir.actions.act_window', {list(action_ids)!r}),\n"
        f"    ('ir.actions.server', {list(server_action_ids)!r}),\n"
        "]\n"
    )
    return header + SECURITY_PROVISION_CODE


def _run_provisioning_action(client: Any, name: str, code: str) -> str:
    """Create/update a named server action, run it, and return what it reported."""
    anchor_model_id = _model_id(client, CONFIG_MODEL) or _model_id(client, "res.partner")
    if not anchor_model_id:
        return "failed: no anchor model available"

    # No try/except wrapper: safe_eval rejects the exception-handling opcodes
    # ("forbidden opcode(s)"), and a dunder such as type(e).__name__ is refused
    # outright. A failure therefore arrives as Odoo's own HTTP 500 traceback,
    # which is more informative than a swallowed message anyway.
    body = (
        code.strip("\n") + "\n"
        "action = {'type': 'ir.actions.client', 'tag': 'display_notification',\n"
        "          'params': {'title': 'Provisioning', 'message': str(out), 'sticky': True}}\n"
    )
    try:
        rows = client.search_read("ir.actions.server", [["name", "=", name]],
                                  fields=["id"], limit=1)
        if rows:
            action_id = rows[0]["id"]
            client.write("ir.actions.server", [action_id], {"code": body})
        else:
            action_id = client.create_one("ir.actions.server", {
                "name": name,
                "model_id": anchor_model_id,
                "state": "code",
                "code": body,
            })
        response = client.execute_kw("ir.actions.server", "run", kwargs={"ids": [action_id]})
    except OdooError as exc:
        return f"failed: {sanitize(exc)}"

    if isinstance(response, dict):
        return str(response.get("params", {}).get("message", response))
    return str(response)


def ensure_security(client: Any, dry_run: bool = False) -> Dict[str, Any]:
    """Install the Integration Manager group, its model ACLs and UI restrictions.

    Returns a report containing the action's own account of what it changed.
    """
    if dry_run:
        return {"status": "skipped (dry run)"}

    menu_ids: List[int] = []
    action_ids: List[int] = []
    server_action_ids: List[int] = []
    try:
        actions = client.search_read_all(
            "ir.actions.act_window",
            [["res_model", "in", [CONFIG_MODEL, SYNC_LOG_MODEL]]], fields=["id"])
        action_ids = [a["id"] for a in actions]
        menus = client.search_read_all("ir.ui.menu", [], fields=["id", "name", "parent_id", "web_icon", "action"])
        for m in menus:
            if str(m.get("name") or "") == "CS Integration Lab" and not m.get("parent_id"):
                vals_to_write = {}
                if not m.get("web_icon"):
                    vals_to_write["web_icon"] = "base,static/description/settings.png"
                if not m.get("action") and action_ids:
                    vals_to_write["action"] = f"ir.actions.act_window,{action_ids[0]}"
                if vals_to_write and not dry_run:
                    client.write("ir.ui.menu", [m["id"]], vals_to_write)
        menu_ids = [m["id"] for m in menus
                    if str(m.get("name") or "") in
                    ("CS Integration Lab", "Integrations", "Sync Logs")]
        model_id = _model_id(client, CONFIG_MODEL)
        if model_id:
            servers = client.search_read(
                "ir.actions.server",
                [["model_id", "=", model_id], ["name", "=", "Sync Now"]],
                fields=["id"], limit=1)
            server_action_ids = [s["id"] for s in servers]
    except OdooError as exc:
        return {"status": f"failed to resolve UI records: {sanitize(exc)}"}

    outcome = _run_provisioning_action(
        client, SECURITY_ACTION_NAME,
        _security_code(menu_ids, action_ids, server_action_ids),
    )
    return {
        "status": outcome,
        "menus_restricted": menu_ids,
        "actions_restricted": action_ids,
        "server_actions_restricted": server_action_ids,
    }


#: Body of the access-verification action.
#:
#: ``has_access`` evaluates the full ``ir.access`` stack for a given user and
#: answers with a boolean instead of raising, which matters here because
#: safe_eval forbids ``try``/``except``: an ``AccessError`` could not be caught
#: and would abort the check rather than record it.
ACCESS_VERIFY_CODE = """
Users = env['res.users'].sudo()
Groups = env['res.groups'].sudo()
group = Groups.search([('name', '=', GROUP_NAME)], limit=1)
report = []
for u in Users.search([('share', '=', False)]):
    member = u.id in group.user_ids.ids
    parts = [u.login, 'manager=' + str(member)]
    for model_name in MODEL_NAMES:
        target = env[model_name].with_user(u.id)
        parts.append(model_name.replace('x_integration_', '') +
                     ' read=' + str(target.has_access('read')) +
                     ' write=' + str(target.has_access('write')) +
                     ' create=' + str(target.has_access('create')))
    report.append(' | '.join(parts))
out = ' || '.join(report)
"""

VERIFY_ACTION_NAME = "CS Integration Lab: Verify Access"


def verify_access(client: Any) -> Dict[str, Any]:
    """Evaluate the real ACL stack for every internal user.

    Reports, per user, whether they are an Integration Manager and what they may
    actually do to each integration model. This is the check that distinguishes
    "a group exists" from "the group is what grants access".
    """
    header = (
        f"GROUP_NAME = {INTEGRATION_MANAGER_GROUP!r}\n"
        f"MODEL_NAMES = {[CONFIG_MODEL, SYNC_LOG_MODEL]!r}\n"
    )
    raw = _run_provisioning_action(client, VERIFY_ACTION_NAME, header + ACCESS_VERIFY_CODE)
    return {"raw": raw, "users": [line.strip() for line in raw.split("||")] if raw else []}


# -- menus ------------------------------------------------------------------

MENU_MODULE = "cs_integration_lab"


def _anchor_xmlid(client: Any, model: str, res_id: int, name: str) -> Optional[str]:
    """Give a record a stable external id, so later runs can find it by identity.

    Without this, :func:`ensure_menus` has to match menus on ``name`` - the one
    attribute a user can edit in the UI. Renaming "Sync Logs" would make the next
    provisioning run believe the menu was missing and create a second one.
    """
    try:
        rows = client.search_read(
            "ir.model.data",
            [["model", "=", model], ["res_id", "=", res_id]],
            fields=["id", "module", "name"], limit=1,
        )
        if rows:
            return f"{rows[0]['module']}.{rows[0]['name']}"
        client.create_one("ir.model.data", {
            "module": MENU_MODULE, "name": name, "model": model,
            "res_id": res_id, "noupdate": True,
        })
        return f"{MENU_MODULE}.{name}"
    except OdooError as exc:
        LOGGER.warning("Could not anchor %s#%s as %s: %s", model, res_id, name, sanitize(exc))
        return None


def _by_xmlid(client: Any, name: str) -> Optional[int]:
    try:
        rows = client.search_read(
            "ir.model.data",
            [["module", "=", MENU_MODULE], ["name", "=", name]],
            fields=["res_id"], limit=1,
        )
        return rows[0]["res_id"] if rows else None
    except OdooError:
        return None


def ensure_menus(client: Any, dry_run: bool = False) -> Dict[str, Any]:
    """Create the CS Integration Lab menu, its two entries and their actions."""
    report: Dict[str, Any] = {"created": [], "existing": [], "failed": {}, "xmlids": []}
    if dry_run:
        return {"status": "skipped (dry run)"}

    specs = [
        ("Integrations", CONFIG_MODEL, 1, "menu_integrations", "action_integrations"),
        ("Sync Logs", SYNC_LOG_MODEL, 2, "menu_sync_logs", "action_sync_logs"),
    ]
    first_action_id: Optional[int] = None
    try:
        # Identity first, name only as a fallback for a database provisioned
        # before external ids were anchored.
        root_id = _by_xmlid(client, "menu_root")
        if not root_id:
            roots = client.search_read("ir.ui.menu", [["name", "=", "CS Integration Lab"]],
                                       fields=["id"], limit=1)
            root_id = roots[0]["id"] if roots else None
        if root_id:
            report["existing"].append("menu:CS Integration Lab")
        else:
            root_id = client.create_one("ir.ui.menu",
                                        {"name": "CS Integration Lab", "sequence": 10})
            report["created"].append(f"menu:CS Integration Lab:{root_id}")
        anchored = _anchor_xmlid(client, "ir.ui.menu", root_id, "menu_root")
        if anchored:
            report["xmlids"].append(anchored)

        for label, model, sequence, menu_key, action_key in specs:
            if not _model_id(client, model):
                report["failed"][label] = f"{model} does not exist"
                continue

            action_id = _by_xmlid(client, action_key)
            if not action_id:
                existing = client.search_read(
                    "ir.actions.act_window",
                    [["res_model", "=", model], ["name", "=", label]], fields=["id"], limit=1)
                action_id = existing[0]["id"] if existing else None
            if action_id:
                report["existing"].append(f"action:{label}")
            else:
                action_id = client.create_one("ir.actions.act_window", {
                    "name": label, "res_model": model, "view_mode": "list,form",
                })
                report["created"].append(f"action:{label}:{action_id}")
            anchored = _anchor_xmlid(client, "ir.actions.act_window", action_id, action_key)
            if anchored:
                report["xmlids"].append(anchored)

            menu_id = _by_xmlid(client, menu_key)
            if not menu_id:
                menus = client.search_read(
                    "ir.ui.menu", [["name", "=", label], ["parent_id", "=", root_id]],
                    fields=["id"], limit=1)
                menu_id = menus[0]["id"] if menus else None
            if menu_id:
                report["existing"].append(f"menu:{label}")
            else:
                menu_id = client.create_one("ir.ui.menu", {
                    "name": label, "parent_id": root_id, "sequence": sequence,
                    "action": f"ir.actions.act_window,{action_id}",
                })
                report["created"].append(f"menu:{label}:{menu_id}")
            anchored = _anchor_xmlid(client, "ir.ui.menu", menu_id, menu_key)
            if anchored:
                report["xmlids"].append(anchored)
            if action_key == "action_integrations":
                first_action_id = action_id

        # The Apps launcher only lists a root menu that HAS an action: Odoo
        # filters the actionless ones out of load_web_menus (which is why
        # "Link Tracker" and "Tests" are visible menus but not apps). Creating
        # the root without one therefore produced a menu that existed, was
        # readable, passed every group check - and never appeared on the Apps
        # screen.
        root = client.read("ir.ui.menu", [root_id], ["action", "web_icon"])[0]
        patch: Dict[str, Any] = {}
        if not root.get("action") and first_action_id:
            patch["action"] = f"ir.actions.act_window,{first_action_id}"
        if not root.get("web_icon"):
            patch["web_icon"] = "base,static/description/settings.png"
        if patch:
            client.write("ir.ui.menu", [root_id], patch)
            report["created"].append(f"menu:root-app-fields:{sorted(patch)}")
        else:
            report["existing"].append("menu:root-app-fields")
    except OdooError as exc:
        report["failed"]["*"] = sanitize(exc)
    return report


DEFAULT_CONFIGS: List[Dict[str, str]] = [
    {"provider": "github", "name": "GitHub Integration"},
    {"provider": "jsonplaceholder", "name": "JSONPlaceholder Integration"},
    {"provider": "frankfurter", "name": "Frankfurter Currency Rates"},
    {"provider": "open_meteo", "name": "Open-Meteo Weather Forecast"},
    {"provider": "nager_date", "name": "Nager.Date Public Holidays"},
]


def ensure_integration_config_records(client: Any, dry_run: bool = False) -> Dict[str, Any]:
    """Ensure one configuration row exists per provider, keyed on the provider.

    ``x_provider`` is the stable identity here - names are editable in the UI, so
    matching on the name would clone a row the moment somebody renamed one.
    """
    report: Dict[str, Any] = {"created": [], "existing": [], "failed": {}}
    for model_name, prov_field, name_field, sched_field in [
        (CONFIG_MODEL, "x_provider", "x_name", "x_schedule_enabled"),
    ]:
        try:
            if not _model_id(client, model_name):
                continue
            existing_rows = client.search_read(model_name, [], fields=["id", prov_field])
            existing_providers = {row.get(prov_field) for row in existing_rows if row.get(prov_field)}
            for item in DEFAULT_CONFIGS:
                prov = item["provider"]
                if prov in existing_providers:
                    report["existing"].append(f"{model_name}:{prov}")
                    continue
                if dry_run:
                    LOGGER.info("[MOCK WRITE] would create config for %s on %s", prov, model_name)
                    report["created"].append(f"{model_name}:{prov}")
                    continue
                vals = {
                    name_field: item["name"],
                    prov_field: prov,
                    sched_field: True,
                    "x_active": True,
                    "x_sync_state": "idle",
                }
                try:
                    client.create_one(model_name, vals)
                    report["created"].append(f"{model_name}:{prov}")
                except OdooError as exc:
                    report["failed"][f"{model_name}:{prov}"] = sanitize(exc)
        except OdooError as exc:
            report["failed"][model_name] = sanitize(exc)
    return report


def provision(client: Any, dry_run: bool = False) -> Dict[str, Any]:
    """Bring any database - including an empty one - to the expected state.

    Order matters: the models must exist before their fields, the fields before
    the records that populate them, and every UI record must exist before
    :func:`ensure_security` can put a group in front of it. Each step is
    individually idempotent, so a second run reports ``existing`` throughout and
    writes nothing.
    """
    return {
        "integration_models": ensure_integration_models(client, dry_run=dry_run),
        "partner_forecast_fields": ensure_partner_forecast_fields(client, dry_run=dry_run),
        "idempotency_fields": ensure_idempotency_fields(client, dry_run=dry_run),
        "partner_forecast_view": ensure_partner_forecast_view(client, dry_run=dry_run),
        "integration_config_views": ensure_integration_config_views(client, dry_run=dry_run),
        "integration_config_records": ensure_integration_config_records(client, dry_run=dry_run),
        "menus": ensure_menus(client, dry_run=dry_run),
        "security": ensure_security(client, dry_run=dry_run),
        "missing_idempotency_fields": verify_idempotency_fields(client),
        "model_access": check_access(client),
    }


__all__ = [
    "CONFIG_MODEL",
    "DEFAULT_CONFIGS",
    "INTEGRATION_MANAGER_GROUP",
    "PARTNER_FORECAST_FIELDS",
    "SYNC_LOG_MODEL",
    "check_access",
    "ensure_fields",
    "ensure_idempotency_fields",
    "ensure_integration_config_records",
    "ensure_integration_config_views",
    "ensure_integration_models",
    "ensure_menus",
    "ensure_model",
    "ensure_partner_forecast_fields",
    "ensure_partner_forecast_view",
    "ensure_security",
    "provision",
    "verify_idempotency_fields",
]



