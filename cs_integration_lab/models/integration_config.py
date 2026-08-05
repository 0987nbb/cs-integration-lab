# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError, AccessError


class IntegrationConfig(models.Model):
    """
    Integration Configuration model for managing external API integrations.
    """
    _name = 'cs.integration.config'
    _description = 'Integration Configuration'
    _order = 'name asc'

    name = fields.Char(string='Name', required=True)
    provider = fields.Selection([
        ('github', 'GitHub'),
        ('jsonplaceholder', 'JSONPlaceholder'),
        ('frankfurter', 'Frankfurter'),
        ('open_meteo', 'Open-Meteo'),
        ('nager_date', 'Nager.Date'),
    ], string='Provider', required=True, default='github')
    active = fields.Boolean(string='Active', default=True)
    schedule_enabled = fields.Boolean(string='Schedule Enabled', default=False)
    last_sync_at = fields.Datetime(string='Last Sync At', readonly=True)
    next_sync_at = fields.Datetime(string='Next Sync At', readonly=True)

    _sql_constraints = [
        ('provider_unique', 'unique(provider)', 'Integration configuration for this provider already exists!')
    ]

    def _check_manager_access(self):
        """Ensure only Integration Managers can perform administrative actions."""
        if not self.env.user.has_group('cs_integration_lab.group_integration_manager'):
            raise AccessError(_("Only Integration Managers are allowed to trigger or modify integration synchronization."))

    def action_sync_now(self):
        """
        Action triggered by 'Sync Now' button.
        Restricted to Integration Managers.
        Creates a log entry indicating provider sync is not yet implemented.
        """
        self.ensure_one()
        self._check_manager_access()

        now = fields.Datetime.now()
        self.write({'last_sync_at': now})

        # Create a foundation log entry without executing fake API calls
        log = self.env['cs.integration.sync.log'].create({
            'name': self.env['ir.sequence'].next_by_code('cs.integration.sync.log') or _('Sync Log'),
            'provider': self.provider,
            'start_time': now,
            'end_time': now,
            'status': 'skipped',
            'created_count': 0,
            'updated_count': 0,
            'skipped_count': 1,
            'failed_count': 0,
            'error_details': _("Sync Now triggered for provider '%s'. Provider-specific synchronization logic is not yet implemented.") % self.provider,
            'config_id': self.id,
        })

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Sync Triggered"),
                'message': _("Sync action initialized for %s. Log entry created (ID: %s).") % (self.provider, log.name),
                'type': 'info',
                'sticky': False,
            }
        }

    @api.model
    def cron_run_scheduled_syncs(self):
        """
        Scheduled action / cron method to run periodic syncs for enabled integrations.
        """
        active_configs = self.search([
            ('active', '=', True),
            ('schedule_enabled', '=', True)
        ])
        for config in active_configs:
            # Reusable hook for provider cron trigger
            now = fields.Datetime.now()
            config.write({'last_sync_at': now})
            self.env['cs.integration.sync.log'].create({
                'name': self.env['ir.sequence'].next_by_code('cs.integration.sync.log') or _('Sync Log'),
                'provider': config.provider,
                'start_time': now,
                'end_time': now,
                'status': 'skipped',
                'skipped_count': 1,
                'error_details': _("Scheduled cron executed for provider '%s'. Integration pending implementation.") % config.provider,
                'config_id': config.id,
            })
