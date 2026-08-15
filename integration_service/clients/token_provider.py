# -*- coding: utf-8 -*-
"""Token provider abstraction separating authentication from Graph operations."""
from __future__ import annotations

import time
from typing import Any, List, Optional, Protocol

import msal

from ..config import M365Settings, get_settings
from ..errors import AuthenticationError, ConfigurationError
from ..sanitize import register_secret, sanitize


class TokenProvider(Protocol):
    """Abstract interface for token acquisition providers."""

    def get_access_token(
        self,
        scopes: Optional[List[str]] = None,
        force_refresh: bool = False,
    ) -> str:
        """Return a valid Bearer access token for the given scopes."""
        ...


class MSALTokenProvider:
    """MSAL ConfidentialClientApplication implementation of TokenProvider."""

    def __init__(
        self,
        settings: Optional[M365Settings] = None,
        msal_app: Optional[Any] = None,
    ) -> None:
        if settings is None:
            settings = get_settings().m365
        self.settings = settings
        self._msal_app = msal_app
        self._cached_token: Optional[str] = None
        self._cached_scopes: List[str] = []
        self._expires_at: float = 0.0

        if self.settings.client_secret:
            register_secret(self.settings.client_secret)

    def _get_msal_app(self) -> msal.ConfidentialClientApplication:
        if self._msal_app is not None:
            return self._msal_app

        if not self.settings.is_configured:
            missing = [
                name
                for name, val in (
                    ("M365_TENANT_ID", self.settings.tenant_id),
                    ("M365_CLIENT_ID", self.settings.client_id),
                    ("M365_CLIENT_SECRET", self.settings.client_secret),
                )
                if not val
            ]
            raise ConfigurationError(
                "Missing required Microsoft Graph settings: "
                + ", ".join(missing)
                + ". Set them in .env before initiating connection."
            )

        self._msal_app = msal.ConfidentialClientApplication(
            client_id=self.settings.client_id,
            client_credential=self.settings.client_secret,
            authority=self.settings.authority,
        )
        return self._msal_app

    def get_access_token(
        self,
        scopes: Optional[List[str]] = None,
        force_refresh: bool = False,
    ) -> str:
        target_scopes = scopes or self.settings.scopes
        now = time.time()
        if (
            not force_refresh
            and self._cached_token
            and self._cached_scopes == target_scopes
            and now < self._expires_at
        ):
            return self._cached_token

        app = self._get_msal_app()
        result = app.acquire_token_for_client(scopes=target_scopes)

        if "access_token" in result:
            token = result["access_token"]
            register_secret(token)
            if "expires_in" in result:
                try:
                    expires_in = int(result.get("expires_in", 0))
                except (TypeError, ValueError):
                    expires_in = 0
                if expires_in > 300:
                    self._cached_token = token
                    self._cached_scopes = list(target_scopes)
                    self._expires_at = now + (expires_in - 300)
                else:
                    self._cached_token = None
                    self._cached_scopes = []
                    self._expires_at = 0.0
            return token

        error_desc = result.get("error_description") or result.get("error") or "Unknown token acquisition error"
        raise AuthenticationError(
            f"Failed to acquire Microsoft Graph access token: {sanitize(error_desc)}",
            status_code=401,
        )


__all__ = ["TokenProvider", "MSALTokenProvider"]
