# -*- coding: utf-8 -*-
"""
Configuration manager for the external RPA Worker.
Loads settings from environment variables with sensible defaults.
"""
import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


@dataclass
class WorkerConfig:
    """Worker configuration container."""
    odoo_url: str = field(default_factory=lambda: os.getenv("ODOO_URL", "https://ai-demo-company.odoo.com"))
    odoo_database: str = field(default_factory=lambda: os.getenv("ODOO_DATABASE", "ai-demo-company"))
    odoo_api_key: str = field(default_factory=lambda: os.getenv("ODOO_API_KEY", ""))
    
    poll_interval_seconds: float = field(default_factory=lambda: float(os.getenv("RPA_POLL_INTERVAL_SECONDS", "5.0")))
    stale_running_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("RPA_STALE_RUNNING_TIMEOUT_SECONDS", "600.0")))
    
    # Playwright settings
    headless: bool = field(default_factory=lambda: os.getenv("RPA_HEADLESS", "true").lower() in ("true", "1", "yes"))
    browser_type: str = field(default_factory=lambda: os.getenv("RPA_BROWSER_TYPE", "chromium"))
    action_timeout_ms: int = field(default_factory=lambda: int(os.getenv("RPA_ACTION_TIMEOUT_MS", "15000")))
    navigation_timeout_ms: int = field(default_factory=lambda: int(os.getenv("RPA_NAV_TIMEOUT_MS", "30000")))
    
    # Model configuration
    model_name: str = field(default_factory=lambda: os.getenv("RPA_MODEL_NAME", "x_rpa_job"))

    # SauceDemo credentials (loaded strictly from environment variables)
    saucedemo_url: str = field(default_factory=lambda: os.getenv("SAUCEDEMO_URL", "https://www.saucedemo.com"))
    saucedemo_username: str = field(default_factory=lambda: os.getenv("SAUCEDEMO_USERNAME", ""))
    saucedemo_password: str = field(default_factory=lambda: os.getenv("SAUCEDEMO_PASSWORD", ""))

    def __post_init__(self) -> None:
        """Registers secret values for automatic process-wide redaction."""
        try:
            from integration_service.sanitize import register_secrets
            register_secrets([self.odoo_api_key, self.saucedemo_password])
        except Exception:
            pass

    def __repr__(self) -> str:
        """Redacts api key and passwords from repr output."""
        return (
            f"WorkerConfig(odoo_url={self.odoo_url!r}, odoo_database={self.odoo_database!r}, "
            f"poll_interval={self.poll_interval_seconds}, stale_timeout={self.stale_running_timeout_seconds}, "
            f"headless={self.headless}, browser={self.browser_type!r}, saucedemo_user={self.saucedemo_username!r}, "
            f"api_key='[REDACTED]', saucedemo_pwd='[REDACTED]')"
        )

