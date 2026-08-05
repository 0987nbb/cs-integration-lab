# -*- coding: utf-8 -*-
import hashlib
import json
from odoo import models, fields, api


class IntegrationIdempotencyMixin(models.AbstractModel):
    """
    Abstract mixin providing reusable fields and utilities for idempotency
    and external record tracking across integrations.
    """
    _name = 'cs.integration.idempotency.mixin'
    _description = 'Integration Idempotency Mixin'

    external_id = fields.Char(
        string='External ID',
        index=True,
        copy=False,
        help='Unique identifier from the external API system.'
    )
    external_updated_at = fields.Datetime(
        string='External Last Updated At',
        copy=False,
        help='Timestamp of last modification in the external system.'
    )
    source_hash = fields.Char(
        string='Source Hash',
        copy=False,
        help='SHA-256 hash of the external record payload to detect changes.'
    )

    @api.model
    def compute_payload_hash(self, payload_dict):
        """
        Compute a consistent SHA-256 hash for a given dictionary payload.
        """
        if not payload_dict:
            return False
        serialized = json.dumps(payload_dict, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()
