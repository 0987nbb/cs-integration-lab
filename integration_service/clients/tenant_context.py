# -*- coding: utf-8 -*-
"""Tenant context abstraction for Microsoft 365.

Decouples Graph business operations from specific tenant configurations,
allowing every operation to carry an explicit tenant context.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..config import M365Settings, get_settings


@dataclass
class TenantContext:
    """Non-secret tenant identity and context metadata."""

    tenant_id: str
    display_name: str = ""
    domain: str = ""
    graph_base_url: str = "https://graph.microsoft.com/v1.0"

    @classmethod
    def from_settings(cls, settings: Optional[M365Settings] = None) -> TenantContext:
        if settings is None:
            settings = get_settings().m365
        return cls(
            tenant_id=settings.tenant_id,
            graph_base_url=settings.graph_base_url,
        )


__all__ = ["TenantContext"]
