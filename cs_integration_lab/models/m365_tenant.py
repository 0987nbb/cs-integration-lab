# -*- coding: utf-8 -*-
"""Odoo Control Plane Models for Microsoft 365 Tenant Readiness & Discovery."""
from __future__ import annotations

import logging
import json
from datetime import datetime, timezone

from odoo import api, fields, models
from odoo.exceptions import UserError

LOGGER = logging.getLogger(__name__)


class CsM365Tenant(models.Model):
    """Microsoft 365 Tenant Record storing discovery metadata and non-secret configuration."""

    _name = 'cs.m365.tenant'
    _description = 'Microsoft 365 Tenant'

    name = fields.Char(string="Tenant Name", required=True, default="Demo Microsoft 365 Tenant")
    tenant_id = fields.Char(string="Tenant ID", index=True, help="Discovered Microsoft Entra Tenant GUID")
    primary_domain = fields.Char(string="Primary Domain")
    domain_count = fields.Integer(string="Domain Count", compute="_compute_domain_count", store=True)

    total_license_capacity = fields.Integer(string="Total Licenses Enabled")
    consumed_license_capacity = fields.Integer(string="Consumed Licenses")
    available_license_capacity = fields.Integer(string="Available Licenses")

    readiness_status = fields.Selection(
        [
            ('draft', 'Draft'),
            ('ready', 'Ready'),
            ('partial', 'Partial'),
            ('failed', 'Failed'),
        ],
        string="Readiness Status",
        default='draft',
        required=True,
    )
    last_readiness_check = fields.Datetime(string="Last Discovery Check")
    last_error = fields.Text(string="Last Error Details")
    discovery_errors = fields.Text(
        string="Discovery Warnings / Errors",
        help="JSON list of read-only discovery areas that were denied or unavailable. Never contains secrets.",
    )

    domain_ids = fields.One2many('cs.m365.domain', 'tenant_record_id', string="Discovered Domains")
    sku_ids = fields.One2many('cs.m365.sku', 'tenant_record_id', string="Subscribed SKUs")
    group_ids = fields.One2many('cs.m365.group', 'tenant_record_id', string="LAB Test Groups")
    capability_ids = fields.One2many('cs.m365.capability', 'tenant_record_id', string="Graph Capabilities")

    @api.depends('domain_ids')
    def _compute_domain_count(self):
        for record in self:
            record.domain_count = len(record.domain_ids)

    def action_check_readiness(self):
        """Execute read-only Microsoft 365 Tenant Readiness Discovery and update records idempotently."""
        self.ensure_one()
        try:
            from integration_service.tenant_readiness import TenantReadinessService
            
            service = TenantReadinessService()
            report = service.run_readiness_check()
            self._update_from_report(report)
            
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Tenant Discovery Complete',
                    'message': f"Discovered tenant {self.primary_domain or self.tenant_id} (Status: {self.readiness_status.upper()})",
                    'type': 'success' if self.readiness_status == 'ready' else 'warning',
                    'sticky': False,
                }
            }
        except Exception as exc:
            self.write({
                'readiness_status': 'failed',
                'last_error': str(exc),
                'last_readiness_check': fields.Datetime.now(),
            })
            raise UserError(f"Tenant Readiness Discovery Failed: {exc}")

    def _update_from_report(self, report):
        """Idempotently update tenant and relational child records from discovery report."""
        now = fields.Datetime.now()
        vals = {
            'tenant_id': report.tenant_id,
            'primary_domain': report.primary_domain,
            'total_license_capacity': report.total_license_capacity,
            'consumed_license_capacity': report.consumed_license_capacity,
            'available_license_capacity': report.available_license_capacity,
            'readiness_status': report.readiness_status,
            'last_readiness_check': now,
            'last_error': report.last_error or False,
            'discovery_errors': json.dumps(report.discovery_errors, ensure_ascii=False) if report.discovery_errors else False,
        }
        if report.display_name and report.display_name != "Discovery Failed":
            vals['name'] = report.display_name

        self.write(vals)

        # 1. Update Domains idempotently using domain_id
        Domain = self.env['cs.m365.domain']
        existing_domains = {d.domain_id: d for d in self.domain_ids}
        seen_domain_ids = set()

        for ddata in report.domains:
            did = ddata.get('domain_id')
            if not did:
                continue
            seen_domain_ids.add(did)
            dvals = {
                'tenant_record_id': self.id,
                'domain_id': did,
                'name': ddata.get('name', did),
                'is_default': ddata.get('is_default', False),
                'is_initial': ddata.get('is_initial', False),
                'status': ddata.get('status', 'Verified'),
            }
            if did in existing_domains:
                existing_domains[did].write(dvals)
            else:
                Domain.create(dvals)

        # 2. Update SKUs idempotently using sku_id
        Sku = self.env['cs.m365.sku']
        existing_skus = {s.sku_id: s for s in self.sku_ids}
        seen_sku_ids = set()

        for sdata in report.skus:
            sid = sdata.get('sku_id')
            if not sid:
                continue
            seen_sku_ids.add(sid)
            svals = {
                'tenant_record_id': self.id,
                'sku_id': sid,
                'sku_part_number': sdata.get('sku_part_number', ''),
                'capability_status': sdata.get('capability_status', 'Enabled'),
                'enabled_units': sdata.get('enabled_units', 0),
                'consumed_units': sdata.get('consumed_units', 0),
                'available_units': sdata.get('available_units', 0),
            }
            if sid in existing_skus:
                existing_skus[sid].write(svals)
            else:
                Sku.create(svals)

        # 3. Update Groups idempotently using graph_id
        Group = self.env['cs.m365.group']
        existing_groups = {g.graph_id: g for g in self.group_ids}
        seen_group_ids = set()

        for gdata in report.lab_groups:
            gid = gdata.get('graph_id')
            if not gid:
                continue
            seen_group_ids.add(gid)
            gvals = {
                'tenant_record_id': self.id,
                'graph_id': gid,
                'name': gdata.get('name', ''),
                'description': gdata.get('description', ''),
                'group_type': gdata.get('group_type', 'Security'),
                'mail_nickname': gdata.get('mail_nickname', ''),
                'is_lab_group': gdata.get('is_lab_group', True),
            }
            if gid in existing_groups:
                existing_groups[gid].write(gvals)
            else:
                Group.create(gvals)

        # 4. Update Capabilities idempotently using capability_key
        Cap = self.env['cs.m365.capability']
        existing_caps = {c.capability_key: c for c in self.capability_ids}

        for cdata in report.capabilities:
            ckey = cdata.get('capability_key')
            if not ckey:
                continue
            cvals = {
                'tenant_record_id': self.id,
                'capability_key': ckey,
                'name': cdata.get('name', ckey),
                'status': cdata.get('status', 'available'),
                'detail': cdata.get('detail', ''),
            }
            if ckey in existing_caps:
                existing_caps[ckey].write(cvals)
            else:
                Cap.create(cvals)


class CsM365Domain(models.Model):
    """Discovered Microsoft 365 Domain."""

    _name = 'cs.m365.domain'
    _description = 'Microsoft 365 Domain'

    tenant_record_id = fields.Many2one('cs.m365.tenant', string="Tenant Record", ondelete='cascade', required=True)
    domain_id = fields.Char(string="Domain ID", required=True, index=True)
    name = fields.Char(string="Domain Name", required=True)
    is_default = fields.Boolean(string="Default Domain")
    is_initial = fields.Boolean(string="Initial Domain")
    status = fields.Char(string="Verification Status")


class CsM365Sku(models.Model):
    """Subscribed Microsoft 365 License SKU."""

    _name = 'cs.m365.sku'
    _description = 'Microsoft 365 Subscribed SKU'

    tenant_record_id = fields.Many2one('cs.m365.tenant', string="Tenant Record", ondelete='cascade', required=True)
    sku_id = fields.Char(string="SKU GUID", required=True, index=True)
    sku_part_number = fields.Char(string="SKU Part Number", required=True)
    capability_status = fields.Char(string="Capability Status")
    enabled_units = fields.Integer(string="Enabled Units")
    consumed_units = fields.Integer(string="Consumed Units")
    available_units = fields.Integer(string="Available Units")


class CsM365Group(models.Model):
    """Configured LAB Test Group."""

    _name = 'cs.m365.group'
    _description = 'Microsoft 365 Test Group'

    tenant_record_id = fields.Many2one('cs.m365.tenant', string="Tenant Record", ondelete='cascade', required=True)
    graph_id = fields.Char(string="Graph Object ID", required=True, index=True)
    name = fields.Char(string="Group Name", required=True)
    description = fields.Text(string="Description")
    group_type = fields.Char(string="Group Type")
    mail_nickname = fields.Char(string="Mail Nickname")
    is_lab_group = fields.Boolean(string="Is LAB Group", default=True)


class CsM365Capability(models.Model):
    """Graph Capability / Permission Status."""

    _name = 'cs.m365.capability'
    _description = 'Microsoft Graph Capability Assessment'

    tenant_record_id = fields.Many2one('cs.m365.tenant', string="Tenant Record", ondelete='cascade', required=True)
    capability_key = fields.Char(string="Capability Key", required=True, index=True)
    name = fields.Char(string="Capability Name", required=True)
    status = fields.Selection(
        [
            ('available', 'Available'),
            ('denied', 'Denied / Insufficient Permission'),
            ('unsupported', 'Not Available / Unsupported'),
            ('failed', 'Failed'),
        ],
        string="Access Status",
        default='available',
        required=True,
    )
    detail = fields.Text(string="Status Detail")


class CsM365UserSnapshot(models.Model):
    """Timestamped Microsoft 365 User 360 Diagnostic Snapshot.

    Each execution of --user-360 creates/updates one record per UPN,
    capturing profile, licenses, groups, auth methods, and devices.
    NEVER stores bearer tokens, client secrets, or access tokens.
    """

    _name = 'cs.m365.user.snapshot'
    _description = 'Microsoft 365 User 360 Diagnostic Snapshot'
    _order = 'snapshot_timestamp desc, id desc'

    name = fields.Char(string="UPN / Reference", required=True, index=True)
    upn = fields.Char(string="User Principal Name", required=True, index=True)
    display_name = fields.Char(string="Display Name")
    graph_id = fields.Char(string="Graph Object ID", index=True)
    account_enabled = fields.Boolean(string="Account Enabled", default=False)
    job_title = fields.Char(string="Job Title")
    department = fields.Char(string="Department")
    usage_location = fields.Char(string="Usage Location")
    manager_upn = fields.Char(string="Manager UPN")
    assigned_licenses = fields.Text(
        string="Assigned Licenses (JSON)",
        help="JSON list of assigned license SKU IDs. Never contains tokens or secrets.",
    )
    direct_groups_count = fields.Integer(string="Direct Groups Count", default=0)
    transitive_groups_count = fields.Integer(string="Transitive Groups Count", default=0)
    auth_methods_status = fields.Char(
        string="Auth Methods Status",
        help="Summary of auth methods availability (e.g. 'Not available', '2 methods registered').",
    )
    devices_count = fields.Integer(string="Registered Devices Count", default=0)
    snapshot_timestamp = fields.Datetime(string="Snapshot Timestamp", index=True)
    diagnostic_status = fields.Selection(
        [
            ('success', 'Success'),
            ('partial', 'Partial'),
            ('failed', 'Failed'),
        ],
        string="Diagnostic Status",
        default='success',
        required=True,
    )
    error_details = fields.Text(
        string="Sanitized Error Details",
        help="Sanitized error output. Never contains access tokens, client secrets, or passwords.",
    )
    notes = fields.Text(
        string="Full Diagnostic JSON",
        help="Complete structured JSON payload from User 360 diagnostic. No credentials stored.",
    )


class CsM365Operation(models.Model):
    """Microsoft 365 Operation Record.

    Tracks onboarding, offboarding, and remediation workflows through their
    full lifecycle from Draft to Verified (or failure states).
    NEVER stores bearer tokens, client secrets, passwords, or access tokens.
    """

    _name = 'cs.m365.operation'
    _description = 'Microsoft 365 Operation'
    _order = 'created_at desc, id desc'

    name = fields.Char(
        string="Reference",
        required=True,
        index=True,
        copy=False,
        default=lambda self: self.env['ir.sequence'].next_by_code('cs.m365.operation') or '/',
    )
    operation_type = fields.Selection(
        [
            ('onboarding', 'Synthetic Onboarding'),
            ('offboarding', 'Synthetic Offboarding'),
            ('remediation', 'Helpdesk Remediation'),
            ('user_360', 'User 360 Diagnostic'),
        ],
        string="Operation Type",
        required=True,
        index=True,
    )
    target_upn = fields.Char(string="Target UPN", index=True)
    target_graph_id = fields.Char(string="Target Graph Object ID")
    requested_by = fields.Char(string="Requested By")
    created_at = fields.Datetime(string="Created At", default=fields.Datetime.now)
    started_at = fields.Datetime(string="Started At")
    finished_at = fields.Datetime(string="Finished At")
    approval_info = fields.Text(string="Approval Information")
    state = fields.Selection(
        [
            ('draft', 'Draft'),
            ('planned', 'Planned'),
            ('awaiting_approval', 'Awaiting Approval'),
            ('running', 'Running'),
            ('verified', 'Verified'),
            ('failed', 'Failed'),
            ('needs_human', 'Needs Human Intervention'),
            ('uncertain', 'Uncertain'),
        ],
        string="State",
        default='draft',
        required=True,
        index=True,
        copy=False,
    )
    planned_mutations = fields.Text(string="Planned Mutations (JSON)")
    execution_result = fields.Text(string="Execution Result / Read-Back Output")
    approved_by = fields.Char(string="Approved By")
    approved_at = fields.Datetime(string="Approved At")
    remediation_action = fields.Char(string="Remediation Action")
    target_group_id = fields.Char(string="Target Group ID")
    target_sku_id = fields.Char(string="Target SKU ID")
    verification_passed = fields.Boolean(string="Verification Passed")

    result = fields.Text(string="Result / Verification Output")
    error_details = fields.Text(
        string="Sanitized Error Details",
        help="Sanitized error output. Never contains access tokens, client secrets, or passwords.",
    )
    last_successful_step = fields.Char(string="Last Successful Step")
    correlation_id = fields.Char(string="Correlation ID")
    notes = fields.Text(
        string="Full Operation JSON",
        help="Complete structured JSON payload from operation execution. No credentials stored.",
    )

    def action_approve(self):
        for op in self:
            if op.state != 'planned':
                raise UserError("Only planned operations can be approved.")
            op.write({
                'state': 'awaiting_approval',
                'approval_info': f"Approved by {self.env.user.name} at {fields.Datetime.now()}",
            })

    def action_reject(self):
        for op in self:
            if op.state not in ['planned', 'awaiting_approval']:
                raise UserError("Cannot reject an operation that is already running or verified.")
            op.write({
                'state': 'failed',
                'error_details': f"Rejected by {self.env.user.name} at {fields.Datetime.now()}",
            })


class CsM365GraphAuditLog(models.Model):
    """Microsoft Graph API Audit Event.

    Records every Microsoft Graph API interaction with enough context to
    diagnose requests. NEVER stores bearer tokens, client secrets,
    passwords, or any sensitive credentials.
    """

    _name = 'cs.m365.graph.audit.log'
    _description = 'Microsoft Graph API Audit Log'
    _order = 'timestamp desc, id desc'

    name = fields.Char(
        string="Audit Entry",
        required=True,
        index=True,
        help="Auto-generated: HTTP Method + Resource path",
    )
    operation_label = fields.Char(
        string="Operation",
        index=True,
        help="Business operation name (e.g. 'Tenant Readiness', 'User 360', 'Onboarding')",
    )
    tenant_display = fields.Char(string="Tenant")
    resource = fields.Char(string="Graph Resource / Endpoint", index=True)
    http_method = fields.Selection(
        [
            ('GET', 'GET'),
            ('POST', 'POST'),
            ('PATCH', 'PATCH'),
            ('PUT', 'PUT'),
            ('DELETE', 'DELETE'),
        ],
        string="HTTP Method",
        index=True,
    )
    graph_request_id = fields.Char(string="Graph Request ID")
    correlation_id = fields.Char(string="Correlation ID")
    timestamp = fields.Datetime(string="Timestamp", index=True)
    http_status = fields.Integer(string="HTTP Status Code")
    success = fields.Boolean(string="Success", default=True)
    sanitized_error = fields.Text(
        string="Sanitized Error",
        help="Sanitized error description. Never contains access tokens or credentials.",
    )
    retry_count = fields.Integer(string="Retry Count", default=0)
    duration_ms = fields.Float(string="Duration (ms)")
