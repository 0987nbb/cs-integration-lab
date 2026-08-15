# -*- coding: utf-8 -*-
"""Provision list/form views for x_m365_* models and create x_m365_snapshot_diff model on Odoo Online."""
import logging
from integration_service.odoo_client import OdooClient
from integration_service.provisioning import (
    ensure_model,
    ensure_fields,
    _model_id,
    _run_provisioning_action,
    INTEGRATION_MANAGER_GROUP,
)

logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger("provision_views_and_diff")

DIFF_MODEL = "x_m365_snapshot_diff"
DIFF_FIELDS = [
    {"name": "x_name", "ttype": "char", "field_description": "Comparison Title", "required": True, "index": True},
    {"name": "x_upn", "ttype": "char", "field_description": "Target UPN", "index": True},
    {"name": "x_timestamp_1", "ttype": "char", "field_description": "Snapshot 1 Timestamp"},
    {"name": "x_timestamp_2", "ttype": "char", "field_description": "Snapshot 2 Timestamp"},
    {"name": "x_field_diffs", "ttype": "text", "field_description": "Field Differences"},
    {"name": "x_added_licenses", "ttype": "text", "field_description": "Licenses Added"},
    {"name": "x_removed_licenses", "ttype": "text", "field_description": "Licenses Removed"},
    {"name": "x_added_groups", "ttype": "text", "field_description": "Groups Added"},
    {"name": "x_removed_groups", "ttype": "text", "field_description": "Groups Removed"},
    {"name": "x_summary", "ttype": "text", "field_description": "Full Comparison Report"},
]

def create_or_update_view(client: OdooClient, name: str, model: str, view_type: str, arch: str):
    existing = client.search_read(
        "ir.ui.view",
        [["name", "=", name], ["model", "=", model]],
        fields=["id"],
        limit=1,
    )
    vals = {
        "name": name,
        "model": model,
        "type": view_type,
        "arch": arch,
        "priority": 16,
    }
    if existing:
        client.write("ir.ui.view", [existing[0]["id"]], vals)
        LOGGER.info("Updated view %s for model %s (id=%s)", name, model, existing[0]["id"])
    else:
        v_id = client.create_one("ir.ui.view", vals)
        LOGGER.info("Created view %s for model %s (id=%s)", name, model, v_id)

def main():
    client = OdooClient()
    LOGGER.info("Connected to Odoo Online (%s, db=%s)", client.url, client.database)

    # 1. Create x_m365_snapshot_diff model
    mid, state = ensure_model(client, DIFF_MODEL, "Microsoft 365 Snapshot Diff Log")
    LOGGER.info("Model %s: %s (id=%s)", DIFF_MODEL, state, mid)
    ensure_fields(client, DIFF_MODEL, DIFF_FIELDS)

    # Access rule for diff model
    sec_code = f"""
Groups = env['res.groups'].sudo()
Access = env['ir.access'].sudo()
Models = env['ir.model'].sudo()

group = Groups.search([('name', '=', '{INTEGRATION_MANAGER_GROUP}')], limit=1)
if not group:
    group = Groups.create({{'name': '{INTEGRATION_MANAGER_GROUP}'}})

m = Models.search([('model', '=', '{DIFF_MODEL}')], limit=1)
if m:
    vals = {{
        'name': '{DIFF_MODEL} / Integration Manager',
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
out = 'ok'
"""
    _run_provisioning_action(client, "CS Integration Lab: Diff Access Provision", sec_code)

    # 2. Provision List Views with Explicit Columns
    # Tenants List View
    tenant_list_arch = """<list string="Microsoft 365 Tenants">
    <field name="x_name"/>
    <field name="x_tenant_id"/>
    <field name="x_primary_domain"/>
    <field name="x_total_licenses"/>
    <field name="x_consumed_licenses"/>
    <field name="x_available_licenses"/>
    <field name="x_readiness_status"/>
    <field name="x_discovery_errors"/>
    <field name="x_last_readiness_check"/>
</list>"""
    create_or_update_view(client, "x_m365_tenant.list", "x_m365_tenant", "list", tenant_list_arch)

    # User Snapshot List View
    snap_list_arch = """<list string="Microsoft 365 User Snapshots">
    <field name="x_name"/>
    <field name="x_upn"/>
    <field name="x_display_name"/>
    <field name="x_job_title"/>
    <field name="x_department"/>
    <field name="x_account_enabled"/>
    <field name="x_manager"/>
    <field name="x_snapshot_timestamp"/>
    <field name="x_diagnostic_status"/>
</list>"""
    create_or_update_view(client, "x_m365_user_snapshot.list", "x_m365_user_snapshot", "list", snap_list_arch)

    # Operation List View
    op_list_arch = """<list string="Microsoft 365 Operations">
    <field name="x_name"/>
    <field name="x_operation_type"/>
    <field name="x_target_upn"/>
    <field name="x_remediation_action"/>
    <field name="x_state"/>
    <field name="x_verification_passed"/>
    <field name="x_created_at"/>
    <field name="x_finished_at"/>
</list>"""
    create_or_update_view(client, "x_m365_operation.list", "x_m365_operation", "list", op_list_arch)

    # Audit Log List View
    audit_list_arch = """<list string="Microsoft Graph Audit Logs"
    decoration-success="x_success == True"
    decoration-danger="x_success == False">
    <field name="x_name"/>
    <field name="x_operation_label"/>
    <field name="x_tenant_display"/>
    <field name="x_resource"/>
    <field name="x_http_method"/>
    <field name="x_graph_request_id"/>
    <field name="x_correlation_id"/>
    <field name="x_timestamp"/>
    <field name="x_http_status"/>
    <field name="x_success"/>
    <field name="x_retry_count"/>
    <field name="x_duration_ms"/>
</list>"""
    create_or_update_view(client, "x_m365_graph_audit_log.list", "x_m365_graph_audit_log", "list", audit_list_arch)

    # Diff Log List View
    diff_list_arch = """<list string="Microsoft 365 Snapshot Diffs">
    <field name="x_name"/>
    <field name="x_upn"/>
    <field name="x_timestamp_1"/>
    <field name="x_timestamp_2"/>
    <field name="x_field_diffs"/>
</list>"""
    create_or_update_view(client, "x_m365_snapshot_diff.list", DIFF_MODEL, "list", diff_list_arch)

    # 3. Create Action & Menu for Snapshot Diffs
    root_menus = client.search_read("ir.ui.menu", [["name", "=", "CS Integration Lab"], ["parent_id", "=", False]], fields=["id"], limit=1)
    root_id = root_menus[0]["id"] if root_menus else False
    m365_menu = client.search_read("ir.ui.menu", [["name", "=", "Microsoft 365"], ["parent_id", "=", root_id]], fields=["id"], limit=1)
    m365_menu_id = m365_menu[0]["id"] if m365_menu else False

    acts = client.search_read("ir.actions.act_window", [["res_model", "=", DIFF_MODEL]], fields=["id"], limit=1)
    if not acts:
        act_id = client.create_one("ir.actions.act_window", {
            "name": "M365 Snapshot Diffs",
            "res_model": DIFF_MODEL,
            "view_mode": "list,form",
        })
    else:
        act_id = acts[0]["id"]

    existing_menu = client.search_read("ir.ui.menu", [["name", "=", "M365 Snapshot Diffs"], ["parent_id", "=", m365_menu_id]], fields=["id"], limit=1)
    if not existing_menu:
        client.create_one("ir.ui.menu", {
            "name": "M365 Snapshot Diffs",
            "parent_id": m365_menu_id,
            "action": f"ir.actions.act_window,{act_id}",
            "sequence": 50,
        })
        LOGGER.info("Created menu M365 Snapshot Diffs")

    LOGGER.info("Provisioning of explicit list views and x_m365_snapshot_diff complete!")

if __name__ == "__main__":
    main()
