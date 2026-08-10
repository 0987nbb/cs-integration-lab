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
        """Ensure users have access to perform integration actions."""
        if not (self.env.user.has_group('cs_integration_lab.group_integration_manager') or self.env.user.has_group('base.group_system') or self.env.is_admin()):
            raise AccessError(_("Only Integration Managers or Administrators can execute manual synchronisation or modify integration settings."))


    def action_sync_now(self):
        """
        Action triggered by 'Sync Now' button.
        Restricted to Integration Managers.
        Executes actual integration sync for the provider and records a detailed sync log.
        """
        self.ensure_one()
        self._check_manager_access()

        start_time = fields.Datetime.now()
        status = 'success'
        error_details = False
        created_count = 0
        updated_count = 0
        skipped_count = 0
        failed_count = 0

        try:
            from integration_service.cli import CONNECTORS
            from integration_service.connectors.base import build_context
            from integration_service.config import get_settings

            settings = get_settings()
            ctx = build_context(settings=settings)
            connector_cls = CONNECTORS.get(self.provider)
            if connector_cls:
                connector = connector_cls(ctx)
                res = connector.run(write_log=False)
                created_count = res.created
                updated_count = res.updated
                skipped_count = res.skipped
                failed_count = res.failed
                status = res.status
                if res.errors:
                    error_details = "\n".join(res.errors)
            else:
                status = 'failed'
                error_details = _("Unknown provider '%s'") % self.provider
        except Exception as exc:
            status = 'failed'
            error_details = str(exc)

        end_time = fields.Datetime.now()
        self.write({'last_sync_at': end_time})

        log = self.env['cs.integration.sync.log'].create({
            'name': self.env['ir.sequence'].next_by_code('cs.integration.sync.log') or _('Sync Log'),
            'provider': self.provider,
            'start_time': start_time,
            'end_time': end_time,
            'status': status,
            'created_count': created_count,
            'updated_count': updated_count,
            'skipped_count': skipped_count,
            'failed_count': failed_count,
            'error_details': error_details,
            'config_id': self.id,
        })

        msg_type = 'success' if status == 'success' else ('warning' if status == 'partial' else 'danger')
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Sync Completed (%s)") % status.upper(),
                'message': _("Sync executed for %s (Created: %s, Updated: %s, Skipped: %s, Failed: %s). Log: %s") % (
                    self.provider, created_count, updated_count, skipped_count, failed_count, log.name
                ),
                'type': msg_type,
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
            config.action_sync_now()

