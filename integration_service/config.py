# -*- coding: utf-8 -*-
"""Environment-backed configuration.

Every credential and every endpoint is read from the process environment, which
``python-dotenv`` populates from ``.env``. Nothing in this package contains a
hardcoded secret, and every secret loaded here is immediately registered with
:mod:`integration_service.sanitize` so that it can never reach a log line.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

from dotenv import load_dotenv

from .errors import ConfigurationError
from .sanitize import register_secrets

load_dotenv()


def _str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None or value == "" else value.strip()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from None


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError:
        raise ConfigurationError(f"{name} must be a number, got {raw!r}") from None


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _csv(name: str, default: str = "") -> List[str]:
    raw = _str(name, default)
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass
class OdooSettings:
    url: str
    database: str
    api_key: str
    timeout: int = 30

    def validate(self) -> None:
        missing = [
            env_name
            for env_name, value in (
                ("ODOO_URL", self.url),
                ("ODOO_DATABASE", self.database),
                ("ODOO_API_KEY", self.api_key),
            )
            if not value
        ]
        if missing:
            raise ConfigurationError(
                "Missing required Odoo settings: "
                + ", ".join(missing)
                + ". Copy .env.example to .env and fill them in."
            )


@dataclass
class HttpSettings:
    timeout: int = 30
    max_retries: int = 4
    backoff_factor: float = 0.5
    backoff_max: float = 30.0
    user_agent: str = "cs-integration-lab/1.0"


@dataclass
class GitHubSettings:
    api_url: str = "https://api.github.com"
    token: str = ""
    owner: str = "octocat"
    repo: str = "Hello-World"
    project_name: str = "GitHub Issues"
    sync_comments: bool = True
    per_page: int = 100

    @property
    def can_write(self) -> bool:
        """Outbound POST/PATCH are only sent when a token is configured.

        Without one the connector still executes its full write path but emits
        ``[MOCK WRITE]`` records instead of contacting GitHub.
        """
        return bool(self.token)

    @property
    def issues_url(self) -> str:
        return f"{self.api_url}/repos/{self.owner}/{self.repo}/issues"


@dataclass
class JsonPlaceholderSettings:
    base_url: str = "https://api.jsonplaceholder.dev"
    fallback_base_url: str = "https://jsonplaceholder.typicode.com"
    post_model: str = "helpdesk.ticket"


@dataclass
class FrankfurterSettings:
    base_url: str = "https://api.frankfurter.dev"
    base_currency: str = "USD"
    quotes: List[str] = field(default_factory=lambda: ["EUR", "GBP", "TRY", "PKR"])
    rate_change_threshold: float = 0.05
    approver_login: str = ""

    @property
    def rates_url(self) -> str:
        return f"{self.base_url}/v2/rates"


@dataclass
class OpenMeteoSettings:
    base_url: str = "https://api.open-meteo.com"
    forecast_days: int = 7
    partner_ids: List[int] = field(default_factory=list)

    @property
    def forecast_url(self) -> str:
        return f"{self.base_url}/v1/forecast"


@dataclass
class NagerSettings:
    base_url: str = "https://date.nager.at"
    countries: List[str] = field(default_factory=lambda: ["US", "DE"])
    years: List[int] = field(default_factory=list)

    def holidays_url(self, country_code: str, year: int) -> str:
        return f"{self.base_url}/api/v4/Holidays/{country_code}/{year}"


@dataclass
class Settings:
    odoo: OdooSettings
    http: HttpSettings
    github: GitHubSettings
    jsonplaceholder: JsonPlaceholderSettings
    frankfurter: FrankfurterSettings
    open_meteo: OpenMeteoSettings
    nager: NagerSettings
    sample_limit: int = 10
    dry_run: bool = False
    sync_log_fallback_path: str = "logs/sync_log.jsonl"


def load_settings() -> Settings:
    """Build :class:`Settings` from the environment and register its secrets."""
    odoo = OdooSettings(
        url=_str("ODOO_URL").rstrip("/"),
        database=_str("ODOO_DATABASE"),
        api_key=_str("ODOO_API_KEY"),
        timeout=_int("ODOO_TIMEOUT", 30),
    )

    github = GitHubSettings(
        api_url=_str("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
        token=_str("GITHUB_TOKEN"),
        owner=_str("GITHUB_OWNER", "octocat"),
        repo=_str("GITHUB_REPO", "Hello-World"),
        project_name=_str("GITHUB_PROJECT_NAME", "GitHub Issues"),
        sync_comments=_bool("GITHUB_SYNC_COMMENTS", True),
        per_page=max(1, min(100, _int("GITHUB_PER_PAGE", 100))),
    )

    nager_years = []
    for raw_year in _csv("NAGER_YEARS"):
        try:
            nager_years.append(int(raw_year))
        except ValueError:
            raise ConfigurationError(f"NAGER_YEARS contains a non-numeric year: {raw_year!r}") from None
    if not nager_years:
        nager_years = [date.today().year]

    partner_ids = []
    for raw_id in _csv("OPEN_METEO_PARTNER_IDS"):
        try:
            partner_ids.append(int(raw_id))
        except ValueError:
            raise ConfigurationError(f"OPEN_METEO_PARTNER_IDS contains a non-numeric id: {raw_id!r}") from None

    settings = Settings(
        odoo=odoo,
        http=HttpSettings(
            timeout=_int("HTTP_TIMEOUT", 30),
            max_retries=_int("HTTP_MAX_RETRIES", 4),
            backoff_factor=_float("HTTP_BACKOFF_FACTOR", 0.5),
            backoff_max=_float("HTTP_BACKOFF_MAX", 30.0),
            user_agent=_str("HTTP_USER_AGENT", "cs-integration-lab/1.0"),
        ),
        github=github,
        jsonplaceholder=JsonPlaceholderSettings(
            base_url=_str("JSONPLACEHOLDER_BASE_URL", "https://api.jsonplaceholder.dev").rstrip("/"),
            fallback_base_url=_str(
                "JSONPLACEHOLDER_FALLBACK_BASE_URL", "https://jsonplaceholder.typicode.com"
            ).rstrip("/"),
            post_model=_str("JSONPLACEHOLDER_POST_MODEL", "helpdesk.ticket"),
        ),
        frankfurter=FrankfurterSettings(
            base_url=_str("FRANKFURTER_BASE_URL", "https://api.frankfurter.dev").rstrip("/"),
            base_currency=_str("FRANKFURTER_BASE_CURRENCY", "USD").upper(),
            quotes=[q.upper() for q in _csv("FRANKFURTER_QUOTES", "EUR,GBP,TRY,PKR")],
            rate_change_threshold=_float("FRANKFURTER_RATE_CHANGE_THRESHOLD", 0.05),
            approver_login=_str("FRANKFURTER_APPROVER_LOGIN"),
        ),
        open_meteo=OpenMeteoSettings(
            base_url=_str("OPEN_METEO_BASE_URL", "https://api.open-meteo.com").rstrip("/"),
            forecast_days=_int("OPEN_METEO_FORECAST_DAYS", 7),
            partner_ids=partner_ids,
        ),
        nager=NagerSettings(
            base_url=_str("NAGER_BASE_URL", "https://date.nager.at").rstrip("/"),
            countries=[c.upper() for c in _csv("NAGER_COUNTRIES", "US,DE")],
            years=nager_years,
        ),
        sample_limit=_int("SYNC_SAMPLE_LIMIT", 10),
        dry_run=_bool("DRY_RUN", False),
        sync_log_fallback_path=_str("SYNC_LOG_FALLBACK_PATH", "logs/sync_log.jsonl"),
    )

    # Anything secret-shaped is masked from every log line and exception message.
    register_secrets([odoo.api_key, github.token])
    return settings


_CACHED: Optional[Settings] = None


def get_settings(refresh: bool = False) -> Settings:
    """Return the process-wide settings, loading them on first use."""
    global _CACHED
    if _CACHED is None or refresh:
        _CACHED = load_settings()
    return _CACHED
