# -*- coding: utf-8 -*-
{
    'name': 'CS Integration Lab',
    'version': '19.0.1.0.0',
    'category': 'Technical',
    'summary': 'Odoo 19 Integration Lab Module Foundation',
    'description': """
        Odoo 19 Module for CS Integration Lab.
        Provides core foundation for external API integrations (GitHub, JSONPlaceholder, Frankfurter, Open-Meteo, Nager.Date),
        including integration configurations, sync logs, security groups, scheduled crons, and idempotency mixins.
    """,
    'author': 'CS Technical Assessment',
    'website': '',
    'depends': ['base', 'mail'],
    'data': [
        'security/groups.xml',
        'security/ir.model.access.csv',
        'data/sequence_data.xml',
        'data/cron_data.xml',
        'views/integration_config_views.xml',
        'views/sync_log_views.xml',
        'views/menus.xml',
    ],
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
