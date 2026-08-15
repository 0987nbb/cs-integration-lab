# -*- coding: utf-8 -*-
"""Provision 4 dedicated M365 Studio models on Odoo Online (ai-demo-company.odoo.com)."""
import logging
import sys
from integration_service.odoo_client import OdooClient
from integration_service.provisioning import (
    ensure_model,
    ensure_fields,
    _model_id,
    _security_code,
    _run_provisioning_action,
    INTEGRATION_MANAGER_GROUP,
)

logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger("provision_m365")

TENANT_MODEL = "x_m365_tenant"
USER_SNAP_MODEL = "x_m365_user_snapshot"
OPERATION_MODEL = "x_m365_operation"
AUDIT_LOG_MODEL = "x_m365_graph_audit_log"

TENANT_FIELDS = [
    {"name": "x_name", "ttype": "char", "field_description": "Tenant Name", "required": True},
    {"name": "x_tenant_id", "ttype": "char", "field_description": "Tenant ID", "index": True},
    {"name": "x_primary_domain", "ttype": "char", "field_description": "Primary Domain"},
    {"name": "x_domains", "ttype": "text", "field_description": "Registered Domains (JSON)"},
    {"name": "x_subscribed_skus", "ttype": "text", "field_description": "Subscribed SKUs (JSON)"},
    {"name": "x_total_licenses", "ttype": "integer", "field_description": "Total Licenses Enabled"},
    {"name": "x_consumed_licenses", "ttype": "integer", "field_description": "Consumed Licenses"},
    {"name": "x_available_licenses", "ttype": "integer", "field_description": "Available Licenses"},
    {"name": "x_lab_groups", "ttype": "text", "field_description": "LAB Test Groups (JSON)"},
    {"name": "x_graph_capabilities", "ttype": "text", "field_description": "Graph Capabilities (JSON)"},
    {"name": "x_discovery_errors", "ttype": "text", "field_description": "Discovery Warnings / Errors (JSON)"},
    {
        "name": "x_readiness_status",
        "ttype": "selection",
        "field_description": "Readiness Status",
        "selection": [("draft", "Draft"), ("ready", "Ready"), ("partial", "Partial"), ("failed", "Failed")],
    },
    {"name": "x_last_readiness_check", "ttype": "datetime", "field_description": "Last Discovery Check"},
    {"name": "x_last_error", "ttype": "text", "field_description": "Last Error Details"},
]

USER_SNAP_FIELDS = [
    {"name": "x_name", "ttype": "char", "field_description": "UPN / Reference", "required": True, "index": True},
    {"name": "x_upn", "ttype": "char", "field_description": "User Principal Name", "required": True, "index": True},
    {"name": "x_display_name", "ttype": "char", "field_description": "Display Name"},
    {"name": "x_graph_id", "ttype": "char", "field_description": "Graph Object ID", "index": True},
    {"name": "x_account_enabled", "ttype": "boolean", "field_description": "Account Enabled"},
    {"name": "x_job_title", "ttype": "char", "field_description": "Job Title"},
    {"name": "x_department", "ttype": "char", "field_description": "Department"},
    {"name": "x_usage_location", "ttype": "char", "field_description": "Usage Location"},
    {"name": "x_manager", "ttype": "char", "field_description": "Manager UPN / Name"},
    {"name": "x_assigned_licenses", "ttype": "text", "field_description": "Assigned Licenses (JSON)"},
    {"name": "x_group_memberships", "ttype": "text", "field_description": "Group Memberships (JSON)"},
    {"name": "x_auth_methods", "ttype": "text", "field_description": "Auth Methods Status"},
    {"name": "x_devices_count", "ttype": "integer", "field_description": "Registered/Managed Devices Count"},
    {"name": "x_snapshot_timestamp", "ttype": "datetime", "field_description": "Snapshot Timestamp", "index": True},
    {
        "name": "x_diagnostic_status",
        "ttype": "selection",
        "field_description": "Diagnostic Status",
        "selection": [("success", "Success"), ("partial", "Partial"), ("failed", "Failed")],
    },
    {"name": "x_error_details", "ttype": "text", "field_description": "Error Details"},
    {"name": "x_notes", "ttype": "text", "field_description": "Full Diagnostic JSON"},
]

OPERATION_FIELDS = [
    {"name": "x_name", "ttype": "char", "field_description": "Reference", "required": True, "index": True},
    {
        "name": "x_operation_type",
        "ttype": "selection",
        "field_description": "Operation Type",
        "selection": [("onboarding", "Synthetic Onboarding"), ("offboarding", "Synthetic Offboarding"), ("remediation", "Helpdesk Remediation"), ("user_360", "User 360 Diagnostic")],
    },
    {"name": "x_target_upn", "ttype": "char", "field_description": "Target UPN", "index": True},
    {"name": "x_target_graph_id", "ttype": "char", "field_description": "Target Graph Object ID"},
    {
        "name": "x_state",
        "ttype": "selection",
        "field_description": "State",
        "selection": [
            ("draft", "Draft"),
            ("planned", "Planned"),
            ("awaiting_approval", "Awaiting Approval"),
            ("running", "Running"),
            ("verified", "Verified"),
            ("failed", "Failed"),
            ("needs_human", "Needs Human"),
            ("uncertain", "Uncertain"),
        ],
    },
    {"name": "x_planned_mutations", "ttype": "text", "field_description": "Planned Mutations (JSON)"},
    {"name": "x_execution_result", "ttype": "text", "field_description": "Execution Result / Read-Back Output"},
    {"name": "x_approved_by", "ttype": "char", "field_description": "Approved By"},
    {"name": "x_approved_at", "ttype": "datetime", "field_description": "Approved At"},
    {"name": "x_remediation_action", "ttype": "char", "field_description": "Remediation Action"},
    {"name": "x_target_group_id", "ttype": "char", "field_description": "Target Group ID"},
    {"name": "x_target_sku_id", "ttype": "char", "field_description": "Target SKU ID"},
    {"name": "x_error_details", "ttype": "text", "field_description": "Error Details"},
    {"name": "x_verification_passed", "ttype": "boolean", "field_description": "Verification Passed"},
    {"name": "x_created_at", "ttype": "datetime", "field_description": "Created At"},
    {"name": "x_started_at", "ttype": "datetime", "field_description": "Started At"},
    {"name": "x_finished_at", "ttype": "datetime", "field_description": "Finished At"},
    {"name": "x_notes", "ttype": "text", "field_description": "Full Operation JSON"},
]

AUDIT_LOG_FIELDS = [
    {"name": "x_name", "ttype": "char", "field_description": "Audit Entry", "required": True, "index": True},
    {"name": "x_operation_label", "ttype": "char", "field_description": "Operation Label", "index": True},
    {"name": "x_tenant_display", "ttype": "char", "field_description": "Tenant"},
    {"name": "x_resource", "ttype": "char", "field_description": "Graph Resource / Endpoint"},
    {
        "name": "x_http_method",
        "ttype": "selection",
        "field_description": "HTTP Method",
        "selection": [("GET", "GET"), ("POST", "POST"), ("PATCH", "PATCH"), ("PUT", "PUT"), ("DELETE", "DELETE")],
    },
    {"name": "x_graph_request_id", "ttype": "char", "field_description": "Graph Request ID"},
    {"name": "x_correlation_id", "ttype": "char", "field_description": "Correlation ID"},
    {"name": "x_timestamp", "ttype": "datetime", "field_description": "Timestamp", "index": True},
    {"name": "x_http_status", "ttype": "integer", "field_description": "HTTP Status"},
    {"name": "x_success", "ttype": "boolean", "field_description": "Success"},
    {"name": "x_sanitized_error", "ttype": "text", "field_description": "Sanitized Error"},
    {"name": "x_retry_count", "ttype": "integer", "field_description": "Retry Count"},
    {"name": "x_duration_ms", "ttype": "float", "field_description": "Duration (ms)"},
]

def main():
    client = OdooClient()
    LOGGER.info("Connected to Odoo Online (%s, db=%s)", client.url, client.database)

    models_to_create = [
        (TENANT_MODEL, "Microsoft 365 Tenant", TENANT_FIELDS),
        (USER_SNAP_MODEL, "Microsoft 365 User Snapshot", USER_SNAP_FIELDS),
        (OPERATION_MODEL, "Microsoft 365 Operation", OPERATION_FIELDS),
        (AUDIT_LOG_MODEL, "Microsoft Graph API Audit Log", AUDIT_LOG_FIELDS),
    ]

    for model_name, description, fields in models_to_create:
        mid, state = ensure_model(client, model_name, description)
        LOGGER.info("Model %s: %s (id=%s)", model_name, state, mid)
        res = ensure_fields(client, model_name, fields)
        LOGGER.info("Fields for %s: created=%s, existing=%s, failed=%s", model_name, res.get("created"), res.get("existing"), res.get("failed"))

    # Provision Security Access Rules (ir.access)
    model_names = [m[0] for m in models_to_create]
    sec_code = f"""
Groups = env['res.groups'].sudo()
Access = env['ir.access'].sudo()
Models = env['ir.model'].sudo()
report = []

group = Groups.search([('name', '=', '{INTEGRATION_MANAGER_GROUP}')], limit=1)
if not group:
    group = Groups.create({{'name': '{INTEGRATION_MANAGER_GROUP}'}})

if env.user.id not in group.user_ids.ids:
    group.write({{'user_ids': [(4, env.user.id)]}})

for m_name in {model_names!r}:
    m = Models.search([('model', '=', m_name)], limit=1)
    if m:
        vals = {{
            'name': m_name + ' / Integration Manager',
            'model_id': m.id,
            'group_id': group.id,
            'operation': 'crud',
            'kind': 'permission',
            'active': True,
        }}
        mine = Access.search([('model_id', '=', m.id), ('group_id', '=', group.id), ('kind', '=', 'permission')], limit=1)
        if mine:
            mine.write(vals)
        else:
            Access.create(vals)
        report.append(m_name + '-access-ok')

out = ' | '.join(report)
"""
    sec_res = _run_provisioning_action(client, "CS Integration Lab: M365 Access Provision", sec_code)
    LOGGER.info("Security Access Result: %s", sec_res)

    # Provision Views, Actions, and Menus
    root_menus = client.search_read("ir.ui.menu", [["name", "=", "CS Integration Lab"], ["parent_id", "=", False]], fields=["id"], limit=1)
    root_id = root_menus[0]["id"] if root_menus else False

    m365_menu = client.search_read("ir.ui.menu", [["name", "=", "Microsoft 365"], ["parent_id", "=", root_id]], fields=["id"], limit=1)
    if not m365_menu:
        m365_menu_id = client.create_one("ir.ui.menu", {
            "name": "Microsoft 365",
            "parent_id": root_id,
            "sequence": 20,
        })
    else:
        m365_menu_id = m365_menu[0]["id"]

    menu_specs = [
        (TENANT_MODEL, "M365 Tenants", 10),
        (USER_SNAP_MODEL, "M365 User Snapshots", 20),
        (OPERATION_MODEL, "M365 Operations", 30),
        (AUDIT_LOG_MODEL, "M365 Graph Audit Logs", 40),
    ]

    for model_name, label, seq in menu_specs:
        acts = client.search_read("ir.actions.act_window", [["res_model", "=", model_name]], fields=["id"], limit=1)
        if not acts:
            act_id = client.create_one("ir.actions.act_window", {
                "name": label,
                "res_model": model_name,
                "view_mode": "list,form",
            })
        else:
            act_id = acts[0]["id"]

        existing_menu = client.search_read("ir.ui.menu", [["name", "=", label], ["parent_id", "=", m365_menu_id]], fields=["id"], limit=1)
        if not existing_menu:
            client.create_one("ir.ui.menu", {
                "name": label,
                "parent_id": m365_menu_id,
                "action": f"ir.actions.act_window,{act_id}",
                "sequence": seq,
            })
            LOGGER.info("Created menu %s -> act_window %s", label, act_id)
        else:
            LOGGER.info("Menu %s already exists", label)

    LOGGER.info("Provisioning of 4 dedicated x_m365_* models complete!")

if __name__ == "__main__":
    main()
