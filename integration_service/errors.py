# -*- coding: utf-8 -*-
"""Exception hierarchy shared by every connector.

Every exception raised by this package carries an already-sanitised message:
:func:`integration_service.sanitize.sanitize` has been applied before the
exception is constructed, so an exception may always be logged verbatim.
"""
from __future__ import annotations

from typing import Optional


class IntegrationError(Exception):
    """Base class for every error raised by the integration service."""


class ConfigurationError(IntegrationError):
    """A required setting is missing or malformed."""


class HttpError(IntegrationError):
    """An outbound HTTP call failed.

    Attributes:
        status_code: HTTP status if a response was received, else ``None``.
        url: request URL with any query-string credentials redacted.
        retryable: whether a retry could plausibly succeed.
        body: truncated, sanitised response body (may be ``None``).
    """

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        url: Optional[str] = None,
        retryable: bool = False,
        body: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url
        self.retryable = retryable
        self.body = body


class TimeoutError_(HttpError):
    """The request exceeded its timeout budget (connect or read)."""

    def __init__(self, message: str, url: Optional[str] = None) -> None:
        super().__init__(message, status_code=None, url=url, retryable=True)


class RateLimitError(HttpError):
    """HTTP 429. ``retry_after`` is the server-advised delay in seconds."""

    def __init__(
        self,
        message: str,
        url: Optional[str] = None,
        retry_after: Optional[float] = None,
        body: Optional[str] = None,
    ) -> None:
        super().__init__(message, status_code=429, url=url, retryable=True, body=body)
        self.retry_after = retry_after


class ServerError(HttpError):
    """HTTP 5xx returned by the upstream service."""

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        url: Optional[str] = None,
        body: Optional[str] = None,
    ) -> None:
        super().__init__(message, status_code=status_code, url=url, retryable=True, body=body)


class ClientError(HttpError):
    """HTTP 4xx (other than 429) returned by the upstream service."""

    def __init__(
        self,
        message: str,
        status_code: int = 400,
        url: Optional[str] = None,
        body: Optional[str] = None,
    ) -> None:
        super().__init__(message, status_code=status_code, url=url, retryable=False, body=body)


class InvalidPayloadError(IntegrationError):
    """The upstream answered successfully but the body is not usable.

    Raised for non-JSON bodies, and for JSON whose shape does not match what the
    connector requires (missing keys, wrong container type, unparseable values).
    """


class OdooError(IntegrationError):
    """An operation against the Odoo JSON-2 API failed."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        model: Optional[str] = None,
        method: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.model = model
        self.method = method

    @property
    def is_access_error(self) -> bool:
        """True when the failure is an Odoo access-rights rejection."""
        return self.status_code in (401, 403)


class RecordSyncError(IntegrationError):
    """A single record failed to sync.

    Raised and caught inside a connector's per-record loop so that one bad
    record is counted as ``failed`` without aborting the remaining records.
    """

    def __init__(self, message: str, external_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.external_id = external_id
