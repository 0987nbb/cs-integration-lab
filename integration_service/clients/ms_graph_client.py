# -*- coding: utf-8 -*-
"""Microsoft Graph API client utilizing MSAL, ResilientHttpClient, and Audit Logger.

Provides modular token acquisition, automatic in-memory token caching,
credential/token redaction via :mod:`integration_service.sanitize`,
structured audit logging, OData `@odata.nextLink` pagination, and
resilient HTTP querying against Microsoft Graph v1.0.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterator, List, Optional, Union

from ..config import M365Settings, get_settings
from ..errors import (
    AuthenticationError,
    AuthorizationError,
    ClientError,
    ConfigurationError,
    GraphProtocolError,
    HttpError,
    InvalidPayloadError,
    RateLimitError,
    ServerError,
    ThrottledError,
    TimeoutError_,
)
from ..http_client import HttpResponse, ResilientHttpClient
from ..sanitize import register_secret, sanitize
from .audit_logger import GraphAuditEvent, GraphAuditLogger
from .tenant_context import TenantContext
from .token_provider import MSALTokenProvider, TokenProvider

LOGGER = logging.getLogger("integration_service.ms_graph")


class MSGraphClient:
    """Client for Microsoft Graph API operations.

    Args:
        tenant_context: TenantContext metadata.
        token_provider: Abstract TokenProvider for acquiring Bearer tokens.
        http_client: ResilientHttpClient instance for HTTP transport.
        audit_logger: GraphAuditLogger instance for structured event logging.
    """

    def __init__(
        self,
        tenant_context: Optional[TenantContext] = None,
        token_provider: Optional[TokenProvider] = None,
        http_client: Optional[ResilientHttpClient] = None,
        audit_logger: Optional[GraphAuditLogger] = None,
        # Backward compatibility / shorthand parameters:
        settings: Optional[M365Settings] = None,
        msal_app: Optional[Any] = None,
    ) -> None:
        if settings is not None:
            self.settings = settings
        else:
            self.settings = get_settings().m365

        self.tenant_context = tenant_context or TenantContext.from_settings(self.settings)

        if token_provider is not None:
            self.token_provider = token_provider
        else:
            self.token_provider = MSALTokenProvider(settings=self.settings, msal_app=msal_app)

        if http_client is not None:
            self.http = http_client
        else:
            full_settings = get_settings()
            self.http = ResilientHttpClient(settings=full_settings.http)

        self.audit_logger = audit_logger or GraphAuditLogger()

    def get_access_token(self, force_refresh: bool = False) -> str:
        """Acquire an access token using the configured TokenProvider."""
        return self.token_provider.get_access_token(force_refresh=force_refresh)

    def request(
        self,
        method: str,
        path_or_url: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Any = None,
        headers: Optional[Dict[str, str]] = None,
        operation_name: Optional[str] = None,
        retryable: bool = True,
        max_retries: Optional[int] = None,
    ) -> HttpResponse:
        """Perform an HTTP request against Microsoft Graph API.

        Handles authentication headers, error classification, request ID correlation,
        and structured audit logging.
        """
        token = self.get_access_token()

        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            url = path_or_url
        else:
            clean_path = path_or_url.lstrip("/")
            url = f"{self.tenant_context.graph_base_url}/{clean_path}"

        req_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if headers:
            req_headers.update(headers)

        op_name = operation_name or f"{method.upper()} {path_or_url.split('?')[0]}"
        start_time = time.time()
        effective_max_retries = max_retries if retryable else 0

        try:
            response = self.http.request(
                method=method,
                url=url,
                params=params,
                json=json_data,
                headers=req_headers,
                max_retries=effective_max_retries,
            )
            duration_ms = (time.time() - start_time) * 1000.0
            retry_count = getattr(self.http, "last_retry_count", 0)

            self.audit_logger.record(
                GraphAuditEvent(
                    operation_name=op_name,
                    http_method=method.upper(),
                    resource_path=path_or_url,
                    tenant_id=self.tenant_context.tenant_id,
                    request_id=response.request_id,
                    correlation_id=response.correlation_id,
                    http_status=response.status_code,
                    success=True,
                    retry_count=retry_count,
                    duration_ms=duration_ms,
                )
            )
            return response

        except RateLimitError as exc:
            duration_ms = (time.time() - start_time) * 1000.0
            retry_count = getattr(self.http, "last_retry_count", 0)
            self.audit_logger.record(
                GraphAuditEvent(
                    operation_name=op_name,
                    http_method=method.upper(),
                    resource_path=path_or_url,
                    tenant_id=self.tenant_context.tenant_id,
                    http_status=429,
                    success=False,
                    retry_count=retry_count,
                    sanitized_error=str(exc),
                    duration_ms=duration_ms,
                )
            )
            raise ThrottledError(
                message=str(exc),
                url=exc.url,
                retry_after=getattr(exc, "retry_after", None),
                body=exc.body,
            ) from None

        except ClientError as exc:
            status = exc.status_code or 400
            if status == 401:
                # Retry once with a fresh token
                try:
                    new_token = self.get_access_token(force_refresh=True)
                    req_headers["Authorization"] = f"Bearer {new_token}"
                    start_time_retry = time.time()
                    retry_response = self.http.request(
                        method=method, url=url, params=params, json=json_data, headers=req_headers, max_retries=effective_max_retries
                    )
                    duration_ms = (time.time() - start_time_retry) * 1000.0
                    retry_count = getattr(self.http, "last_retry_count", 0) + 1
                    self.audit_logger.record(
                        GraphAuditEvent(
                            operation_name=op_name, http_method=method.upper(), resource_path=path_or_url,
                            tenant_id=self.tenant_context.tenant_id, request_id=retry_response.request_id,
                            correlation_id=retry_response.correlation_id,
                            http_status=retry_response.status_code, success=True, retry_count=retry_count, duration_ms=duration_ms,
                        )
                    )
                    return retry_response
                except Exception as retry_exc:
                    duration_ms = (time.time() - start_time) * 1000.0
                    retry_count = getattr(self.http, "last_retry_count", 0) + 1
                    self.audit_logger.record(
                        GraphAuditEvent(
                            operation_name=op_name, http_method=method.upper(), resource_path=path_or_url,
                            tenant_id=self.tenant_context.tenant_id, http_status=401, success=False,
                            retry_count=retry_count, sanitized_error=str(retry_exc), duration_ms=duration_ms,
                        )
                    )
                    raise AuthenticationError(message=str(retry_exc), status_code=401) from None

            duration_ms = (time.time() - start_time) * 1000.0
            retry_count = getattr(self.http, "last_retry_count", 0)
            self.audit_logger.record(
                GraphAuditEvent(
                    operation_name=op_name,
                    http_method=method.upper(),
                    resource_path=path_or_url,
                    tenant_id=self.tenant_context.tenant_id,
                    http_status=status,
                    success=False,
                    retry_count=retry_count,
                    sanitized_error=str(exc),
                    duration_ms=duration_ms,
                )
            )
            if status == 403:
                raise AuthorizationError(message=str(exc), status_code=403, url=exc.url, body=exc.body) from None
            raise

        except HttpError as exc:
            duration_ms = (time.time() - start_time) * 1000.0
            retry_count = getattr(self.http, "last_retry_count", 0)
            self.audit_logger.record(
                GraphAuditEvent(
                    operation_name=op_name,
                    http_method=method.upper(),
                    resource_path=path_or_url,
                    tenant_id=self.tenant_context.tenant_id,
                    http_status=exc.status_code,
                    success=False,
                    retry_count=retry_count,
                    sanitized_error=str(exc),
                    duration_ms=duration_ms,
                )
            )
            raise

        except Exception as exc:
            duration_ms = (time.time() - start_time) * 1000.0
            retry_count = getattr(self.http, "last_retry_count", 0)
            self.audit_logger.record(
                GraphAuditEvent(
                    operation_name=op_name,
                    http_method=method.upper(),
                    resource_path=path_or_url,
                    tenant_id=self.tenant_context.tenant_id,
                    success=False,
                    retry_count=retry_count,
                    sanitized_error=str(exc),
                    duration_ms=duration_ms,
                )
            )
            raise

    def get(self, path_or_url: str, **kwargs: Any) -> HttpResponse:
        return self.request("GET", path_or_url, **kwargs)

    def post(self, path_or_url: str, json_data: Any = None, **kwargs: Any) -> HttpResponse:
        return self.request("POST", path_or_url, json_data=json_data, **kwargs)

    def put(self, path_or_url: str, json_data: Any = None, **kwargs: Any) -> HttpResponse:
        return self.request("PUT", path_or_url, json_data=json_data, **kwargs)

    def patch(self, path_or_url: str, json_data: Any = None, **kwargs: Any) -> HttpResponse:
        return self.request("PATCH", path_or_url, json_data=json_data, **kwargs)

    def delete(self, path_or_url: str, **kwargs: Any) -> HttpResponse:
        return self.request("DELETE", path_or_url, **kwargs)

    def paginate(
        self,
        path_or_url: str,
        params: Optional[Dict[str, Any]] = None,
        max_pages: int = 100,
        operation_name: str = "PAGINATE",
    ) -> Iterator[Dict[str, Any]]:
        """Yield individual item dictionaries across OData `@odata.nextLink` pages.

        Follows `@odata.nextLink` automatically up to `max_pages`.
        Raises `GraphProtocolError` if response data shape is invalid.
        """
        next_url: Optional[str] = path_or_url
        next_params = dict(params) if params else None
        pages = 0
        seen_urls: set = set()

        while next_url and pages < max_pages:
            if next_url in seen_urls:
                LOGGER.warning("Pagination loop detected at %s; stopping.", sanitize(next_url))
                break
            seen_urls.add(next_url)

            response = self.get(next_url, params=next_params, operation_name=operation_name)
            pages += 1
            next_params = None  # @odata.nextLink already contains query parameters

            if not isinstance(response.data, dict):
                raise GraphProtocolError(
                    f"Graph API pagination error: expected JSON object from {sanitize(next_url)}, got {type(response.data).__name__}"
                )

            items = response.data.get("value")
            if items is None:
                if "raw" in response.data or response.is_empty:
                    items = []
                else:
                    raise GraphProtocolError(
                        f"Graph API pagination error: response missing 'value' field from {sanitize(next_url)}"
                    )

            if not isinstance(items, list):
                raise GraphProtocolError(
                    f"Graph API pagination error: 'value' field is not a list in response from {sanitize(next_url)}"
                )

            for item in items:
                yield item

            next_url = response.data.get("@odata.nextLink")

    def get_users(self, top: int = 5) -> Dict[str, Any]:
        """Fetch users from Microsoft Graph API (`GET /users?$top=N`)."""
        response = self.get("users", params={"$top": top}, operation_name="GET_USERS")
        if isinstance(response.data, dict):
            return response.data
        return {"value": [], "raw": response.text}

    def paginate_users(self, top: int = 5, max_pages: int = 100) -> Iterator[Dict[str, Any]]:
        """Yield users across all pages up to `max_pages`."""
        return self.paginate("users", params={"$top": top}, max_pages=max_pages, operation_name="PAGINATE_USERS")


__all__ = ["MSGraphClient"]
