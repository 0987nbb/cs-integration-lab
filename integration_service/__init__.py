# -*- coding: utf-8 -*-
"""External Python integration service for the Odoo 19 Integration Lab.

Consumes five public APIs and synchronises them into an Odoo 19 Online database
through the JSON-2 REST API (Bearer token). No Odoo addon is installed on the
target; every custom field is provisioned through the API.
"""

__version__ = "1.0.0"

__all__ = [
    "config",
    "connectors",
    "errors",
    "http_client",
    "idempotency",
    "odoo_client",
    "provisioning",
    "sanitize",
    "sync_logger",
    "sync_result",
]
