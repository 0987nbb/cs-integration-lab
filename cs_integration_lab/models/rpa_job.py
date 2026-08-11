# -*- coding: utf-8 -*-
import json
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError


class RpaJob(models.Model):
    """
    Model for managing Odoo Browser Automation / RPA Jobs.
    Acts as the control and queuing layer for external Playwright RPA workers.
    """
    _name = 'cs.rpa.job'
    _description = 'Browser Automation / RPA Job'
    _inherit = ['mail.thread', 'cs.integration.idempotency.mixin']
    _order = 'create_date desc, id desc'

    name = fields.Char(
        string='Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New'),
        index=True
    )
    job_type = fields.Selection([
        ('saucedemo', 'SauceDemo E2E Checkout'),
        ('ui_playground', 'UI Testing Playground'),
    ], string='Job Type', required=True, default='saucedemo', tracking=True)

    payload = fields.Text(
        string='Input Payload',
        required=True,
        help='JSON formatted input parameters for the automation job.'
    )
    state = fields.Selection([
        ('draft', 'Draft'),
        ('queued', 'Queued'),
        ('running', 'Running'),
        ('success', 'Success'),
        ('failed', 'Failed'),
        ('needs_human', 'Needs Human Intervention'),
    ], string='State', required=True, default='draft', copy=False, index=True, tracking=True)

    idempotency_key = fields.Char(
        string='Idempotency Key',
        required=True,
        index=True,
        copy=False,
        help='Unique key to prevent duplicate job execution.'
    )
    attempt_count = fields.Integer(
        string='Attempt Count',
        default=0,
        copy=False,
        readonly=True
    )
    started_at = fields.Datetime(
        string='Started At',
        copy=False,
        readonly=True
    )
    finished_at = fields.Datetime(
        string='Finished At',
        copy=False,
        readonly=True
    )
    last_successful_step = fields.Char(
        string='Last Successful Step',
        copy=False
    )
    result = fields.Text(
        string='Result',
        copy=False
    )
    error_details = fields.Text(
        string='Error Details',
        copy=False
    )
    screenshot = fields.Binary(
        string='Screenshot / Evidence',
        attachment=True,
        copy=False
    )
    screenshot_filename = fields.Char(
        string='Screenshot Filename',
        copy=False
    )
    external_reference = fields.Char(
        string='External Reference',
        copy=False,
        help='Reference from external automation target (e.g. Order ID).'
    )

    _sql_constraints = [
        ('idempotency_key_unique', 'unique(idempotency_key)', 'The Idempotency Key must be unique across all RPA jobs!')
    ]

    @api.constrains('idempotency_key')
    def _check_idempotency_key(self):
        for record in self:
            if not record.idempotency_key or not str(record.idempotency_key).strip():
                raise ValidationError(_("Idempotency key is required and cannot be empty or whitespace-only."))
            key = record.idempotency_key.strip()
            domain = [('idempotency_key', '=', key), ('id', '!=', record.id)]
            if self.search_count(domain) > 0:
                raise ValidationError(_("Idempotency key '%s' is already in use by another RPA job.") % key)

    @api.constrains('payload')
    def _check_payload(self):
        for record in self:
            if not record.payload or not str(record.payload).strip():
                raise ValidationError(_("Input payload is required and cannot be empty or whitespace-only."))
            try:
                json.loads(record.payload)
            except (ValueError, TypeError) as exc:
                raise ValidationError(_("Input payload must be valid JSON format: %s") % str(exc))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('cs.rpa.job') or _('New')

            # Clean surrounding whitespace from idempotency_key if provided
            if 'idempotency_key' in vals and isinstance(vals['idempotency_key'], str):
                vals['idempotency_key'] = vals['idempotency_key'].strip()

            # Enforce job_type presence
            if not vals.get('job_type'):
                raise ValidationError(_("Job Type is required and cannot be empty."))

            # Enforce idempotency_key presence & non-whitespace
            key = vals.get('idempotency_key')
            if not key or not str(key).strip():
                raise ValidationError(_("Idempotency key is required and cannot be empty or whitespace-only."))

            # Enforce payload presence, non-whitespace, and valid JSON
            payload = vals.get('payload')
            if not payload or not str(payload).strip():
                raise ValidationError(_("Input payload is required and cannot be empty or whitespace-only."))
            try:
                json.loads(payload)
            except (ValueError, TypeError) as exc:
                raise ValidationError(_("Input payload must be valid JSON format: %s") % str(exc))

            # Initial state validation
            state = vals.get('state', 'draft')
            if state not in ('draft', 'queued'):
                raise ValidationError(_("New RPA jobs must be created in 'draft' or 'queued' state, got '%s'.") % state)

        return super(RpaJob, self).create(vals_list)

    def write(self, vals):
        if 'idempotency_key' in vals and isinstance(vals['idempotency_key'], str):
            vals['idempotency_key'] = vals['idempotency_key'].strip()

        if 'payload' in vals:
            payload = vals['payload']
            if not payload or not str(payload).strip():
                raise ValidationError(_("Input payload is required and cannot be empty or whitespace-only."))
            try:
                json.loads(payload)
            except (ValueError, TypeError) as exc:
                raise ValidationError(_("Input payload must be valid JSON format: %s") % str(exc))

        if 'idempotency_key' in vals:
            key = vals['idempotency_key']
            if not key or not str(key).strip():
                raise ValidationError(_("Idempotency key is required and cannot be empty or whitespace-only."))

        # Validate allowed state transitions if state is being updated
        if 'state' in vals:
            allowed_transitions = {
                'draft': ['queued'],
                'queued': ['running'],
                'running': ['success', 'failed', 'needs_human'],
                'failed': ['queued'],
                'needs_human': ['queued'],
                'success': [],  # Terminal state
            }
            for record in self:
                current_state = record.state
                target_state = vals['state']
                if target_state not in allowed_transitions.get(current_state, []):
                    raise ValidationError(
                        _("Unsafe state transition for job %s: cannot move directly from '%s' to '%s'.") % (
                            record.name, current_state, target_state
                        )
                    )
        return super(RpaJob, self).write(vals)

    def action_claim_job_atomic(self):
        """
        Atomically claims a queued job inside a single server-side PostgreSQL transaction.
        Ensures that only ONE process can transition a job from 'queued' to 'running'.
        """
        self.ensure_one()
        if self.state != 'queued':
            return False
        now = fields.Datetime.now()
        self.write({
            'state': 'running',
            'started_at': now,
            'attempt_count': self.attempt_count + 1,
        })
        return self.read()[0]

    def action_queue(self):
        """
        Move draft job to queued state after validating input payload and idempotency key.
        This does NOT execute Playwright inside Odoo. It enqueues the job for external worker pickup.
        """
        for record in self:
            if record.state != 'draft':
                raise UserError(_("Only draft jobs can be queued. Job %s is currently in state '%s'.") % (record.name, record.state))
            if not record.idempotency_key or not record.idempotency_key.strip():
                raise UserError(_("Idempotency key must be provided when queueing job %s.") % record.name)
            if not record.payload or not record.payload.strip():
                raise UserError(_("Input payload cannot be empty when queueing job %s.") % record.name)
            
            try:
                json.loads(record.payload)
            except (ValueError, TypeError) as exc:
                raise UserError(_("Input payload for job %s must be valid JSON: %s") % (record.name, str(exc)))

            vals = {'state': 'queued'}
            if record.name == _('New') or not record.name:
                vals['name'] = self.env['ir.sequence'].next_by_code('cs.rpa.job') or _('New')
            record.write(vals)
        return True

    def action_retry(self):
        """
        Retry failed or needs_human jobs by returning them to queued state.
        Increments attempt_count and clears previous error_details while preserving execution history.
        """
        for record in self:
            if record.state not in ('failed', 'needs_human'):
                raise UserError(_("Only failed or needs_human jobs can be retried. Job %s is currently in state '%s'.") % (record.name, record.state))
            record.write({
                'state': 'queued',
                'attempt_count': record.attempt_count + 1,
                'error_details': False,
            })
        return True
