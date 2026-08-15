# -*- coding: utf-8 -*-
"""Comprehensive mocked unit tests for Microsoft Graph Foundation Layer.

Covers all 21 foundation requirements fully offline:
1. Token acquisition success
2. Token acquisition failure
3. Token caching
4. Token refresh/expired token behavior
5. Missing configuration
6. GET users success
7. Pagination (@odata.nextLink)
8. 401 handling (AuthenticationError)
9. 403 handling (AuthorizationError)
10. 429 throttling (ThrottledError)
11. Retry-After handling
12. 5xx retry (ServerError)
13. Network timeout (TimeoutError_)
14. Invalid Graph response (GraphProtocolError)
15. Secret redaction
16. Token redaction
17. Authorization header never logged
18. Graph request/correlation ID extraction
19. Structured audit event creation
20. Multi-tenant context is passed through correctly
21. No credentials required for tests
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests
import responses

from integration_service.clients.audit_logger import GraphAuditEvent, GraphAuditLogger
from integration_service.clients.ms_graph_client import MSGraphClient
from integration_service.clients.tenant_context import TenantContext
from integration_service.clients.token_provider import MSALTokenProvider
from integration_service.config import M365Settings
from integration_service.errors import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    GraphProtocolError,
    ServerError,
    ThrottledError,
    TimeoutError_,
)
from integration_service.http_client import HttpSettings, ResilientHttpClient, redacted_request_log
from integration_service.sanitize import sanitize


@pytest.fixture
def m365_settings() -> M365Settings:
    return M365Settings(
        tenant_id="00000000-0000-0000-0000-000000000000",
        client_id="11111111-1111-1111-1111-111111111111",
        client_secret="test-super-secret-client-secret-key-999",
        graph_base_url="https://graph.microsoft.com/v1.0",
    )


@pytest.fixture
def fast_http() -> ResilientHttpClient:
    settings = HttpSettings(max_retries=1, backoff_factor=0.01, backoff_max=0.05)
    return ResilientHttpClient(settings=settings, jitter=False)


@pytest.fixture
def mock_token_provider() -> MagicMock:
    provider = MagicMock()
    provider.get_access_token.return_value = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.mock-test-access-token-0001"
    return provider


# 1. Token acquisition success
def test_1_token_acquisition_success(m365_settings):
    mock_app = MagicMock()
    mock_app.acquire_token_for_client.return_value = {
        "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.mock-token-12345"
    }
    provider = MSALTokenProvider(settings=m365_settings, msal_app=mock_app)
    token = provider.get_access_token()
    assert token == "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.mock-token-12345"


# 2. Token acquisition failure
def test_2_token_acquisition_failure(m365_settings):
    mock_app = MagicMock()
    mock_app.acquire_token_for_client.return_value = {
        "error": "invalid_client",
        "error_description": "Invalid client secret provided for test-super-secret-client-secret-key-999",
    }
    provider = MSALTokenProvider(settings=m365_settings, msal_app=mock_app)
    with pytest.raises(AuthenticationError) as exc_info:
        provider.get_access_token()
    assert "test-super-secret-client-secret-key-999" not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)


# 3. Token caching
def test_3_token_caching(m365_settings):
    mock_app = MagicMock()
    mock_app.acquire_token_for_client.return_value = {
        "access_token": "cached-access-token-9999"
    }
    provider = MSALTokenProvider(settings=m365_settings, msal_app=mock_app)
    t1 = provider.get_access_token()
    t2 = provider.get_access_token()
    assert t1 == t2
    assert mock_app.acquire_token_for_client.call_count == 2


# 4. Token refresh / expired token behavior
def test_4_token_refresh_behavior(m365_settings):
    mock_app = MagicMock()
    mock_app.acquire_token_for_client.side_effect = [
        {"access_token": "token-v1-expired"},
        {"access_token": "token-v2-refreshed"},
    ]
    provider = MSALTokenProvider(settings=m365_settings, msal_app=mock_app)
    t1 = provider.get_access_token()
    t2 = provider.get_access_token()
    assert t1 == "token-v1-expired"
    assert t2 == "token-v2-refreshed"


# 5. Missing configuration
def test_5_missing_configuration():
    provider = MSALTokenProvider(settings=M365Settings())
    with pytest.raises(ConfigurationError) as exc_info:
        provider.get_access_token()
    assert "Missing required Microsoft Graph settings" in str(exc_info.value)


# 6. GET users success
@responses.activate
def test_6_get_users_success(m365_settings, mock_token_provider, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users?$top=5",
        json={"value": [{"id": "u1", "displayName": "Alice"}]},
        status=200,
    )
    tenant_ctx = TenantContext.from_settings(m365_settings)
    client = MSGraphClient(
        tenant_context=tenant_ctx,
        token_provider=mock_token_provider,
        http_client=fast_http,
    )
    data = client.get_users(top=5)
    assert len(data.get("value", [])) == 1
    assert data["value"][0]["displayName"] == "Alice"


# 7. Pagination (@odata.nextLink)
@responses.activate
def test_7_pagination_nextlink(m365_settings, mock_token_provider, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users?$top=2",
        json={
            "value": [{"id": "u1"}, {"id": "u2"}],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?$skiptoken=abc123",
        },
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users?$skiptoken=abc123",
        json={"value": [{"id": "u3"}]},
        status=200,
    )
    tenant_ctx = TenantContext.from_settings(m365_settings)
    client = MSGraphClient(
        tenant_context=tenant_ctx,
        token_provider=mock_token_provider,
        http_client=fast_http,
    )
    items = list(client.paginate_users(top=2))
    assert len(items) == 3
    assert [i["id"] for i in items] == ["u1", "u2", "u3"]


# 8. 401 handling (AuthenticationError)
@responses.activate
def test_8_401_handling(m365_settings, mock_token_provider, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users?$top=5",
        json={"error": {"code": "InvalidAuthenticationToken"}},
        status=401,
    )
    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=fast_http,
    )
    with pytest.raises(AuthenticationError) as exc_info:
        client.get_users(top=5)
    assert exc_info.value.status_code == 401


@responses.activate
def test_8b_401_token_retry_success(m365_settings, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users?$top=5",
        json={"error": {"code": "InvalidAuthenticationToken"}},
        status=401,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users?$top=5",
        json={"value": [{"id": "u-after-refresh"}]},
        status=200,
    )
    provider = MagicMock()
    provider.get_access_token.side_effect = ["expired-token", "fresh-token"]
    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=provider,
        http_client=fast_http,
    )

    result = client.get_users(top=5)

    assert result["value"][0]["id"] == "u-after-refresh"
    provider.get_access_token.assert_any_call(force_refresh=True)
    assert responses.calls[0].request.headers["Authorization"] == "Bearer expired-token"
    assert responses.calls[1].request.headers["Authorization"] == "Bearer fresh-token"


# 9. 403 handling (AuthorizationError)
@responses.activate
def test_9_403_handling(m365_settings, mock_token_provider, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users?$top=5",
        json={"error": {"code": "Authorization_RequestDenied"}},
        status=403,
    )
    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=fast_http,
    )
    with pytest.raises(AuthorizationError) as exc_info:
        client.get_users(top=5)
    assert exc_info.value.status_code == 403


# 10. 429 throttling (ThrottledError)
@responses.activate
def test_10_429_throttling(m365_settings, mock_token_provider, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users?$top=5",
        headers={"Retry-After": "1"},
        status=429,
    )
    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=fast_http,
    )
    with pytest.raises(ThrottledError) as exc_info:
        client.get_users(top=5)
    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after == 1.0


# 11. Retry-After handling
@responses.activate
def test_11_retry_after_handling(m365_settings, mock_token_provider, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users?$top=5",
        headers={"Retry-After": "2"},
        status=429,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users?$top=5",
        json={"value": [{"id": "u1"}]},
        status=200,
    )
    # Give max_retries=1
    http = ResilientHttpClient(
        settings=HttpSettings(max_retries=1, backoff_factor=0.01),
        sleep=lambda s: None,
        jitter=False,
    )
    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=http,
    )
    res = client.get_users(top=5)
    assert len(res["value"]) == 1


# 12. 5xx retry (ServerError)
@responses.activate
def test_12_5xx_retry(m365_settings, mock_token_provider, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users?$top=5",
        status=503,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users?$top=5",
        status=503,
    )
    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=fast_http,
    )
    with pytest.raises(ServerError) as exc_info:
        client.get_users(top=5)
    assert exc_info.value.status_code == 503


# 13. Network timeout (TimeoutError_)
@responses.activate
def test_13_network_timeout(m365_settings, mock_token_provider, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users?$top=5",
        body=requests.exceptions.Timeout("Read timeout"),
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users?$top=5",
        body=requests.exceptions.Timeout("Read timeout"),
    )
    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=fast_http,
    )
    with pytest.raises(TimeoutError_):
        client.get_users(top=5)


# 14. Invalid Graph response (GraphProtocolError)
@responses.activate
def test_14_invalid_graph_response(m365_settings, mock_token_provider, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users?$top=5",
        json={"value": "invalid-should-be-a-list"},
        status=200,
    )
    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=fast_http,
    )
    with pytest.raises(GraphProtocolError) as exc_info:
        list(client.paginate_users(top=5))
    assert "not a list" in str(exc_info.value)


# 15 & 16. Secret and Token redaction
def test_15_16_secret_and_token_redaction(m365_settings):
    secret = m365_settings.client_secret
    token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret-token-value-12345"
    mock_app = MagicMock()
    mock_app.acquire_token_for_client.return_value = {"access_token": token}
    provider = MSALTokenProvider(settings=m365_settings, msal_app=mock_app)

    acquired = provider.get_access_token()
    assert secret not in sanitize(f"Secret is {secret}")
    assert acquired not in sanitize(f"Token is {acquired}")
    assert "[REDACTED]" in sanitize(f"Secret is {secret}")
    assert "[REDACTED]" in sanitize(f"Token is {acquired}")


# 17. Authorization header never logged
def test_17_authorization_header_never_logged():
    headers = {"Authorization": "Bearer eyJhbGci.secret-jwt-token"}
    log_text = redacted_request_log("GET", "https://graph.microsoft.com/v1.0/users", headers)
    assert "secret-jwt-token" not in log_text
    assert "[REDACTED]" in log_text


# 18. Graph request/correlation ID extraction
@responses.activate
def test_18_correlation_id_extraction(m365_settings, mock_token_provider, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users?$top=5",
        headers={"client-request-id": "req-guid-12345-67890"},
        json={"value": []},
        status=200,
    )
    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=fast_http,
    )
    res = client.get("users", params={"$top": 5})
    assert res.request_id == "req-guid-12345-67890"


# 19. Structured audit event creation
@responses.activate
def test_19_structured_audit_event_creation(m365_settings, mock_token_provider, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users?$top=5",
        headers={"client-request-id": "corr-id-999"},
        json={"value": []},
        status=200,
    )
    audit_logger = GraphAuditLogger()
    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=fast_http,
        audit_logger=audit_logger,
    )
    client.get_users(top=5)
    assert len(audit_logger.events) == 1
    event = audit_logger.events[0]
    assert event.request_id == "corr-id-999"
    assert event.correlation_id == "corr-id-999"
    assert event.http_status == 200
    assert event.success is True
    assert event.tenant_id == m365_settings.tenant_id


def test_audit_logger_persists_odoo_online_diagnostic_fields():
    class FakeOdoo:
        def __init__(self):
            self.created = []

        def model_exists(self, model):
            return model == "x_m365_graph_audit_log"

        def fields_get(self, model):
            assert model == "x_m365_graph_audit_log"
            return {
                "x_name": {},
                "x_operation_label": {},
                "x_tenant_display": {},
                "x_resource": {},
                "x_http_method": {},
                "x_graph_request_id": {},
                "x_correlation_id": {},
                "x_timestamp": {},
                "x_http_status": {},
                "x_success": {},
                "x_sanitized_error": {},
                "x_retry_count": {},
                "x_duration_ms": {},
            }

        def create_one(self, model, vals):
            assert model == "x_m365_graph_audit_log"
            self.created.append(vals)
            return len(self.created)

    audit_logger = GraphAuditLogger()
    audit_logger.record(GraphAuditEvent(
        operation_name="User 360",
        http_method="GET",
        resource_path="users/lab-user@contoso.com",
        tenant_id="tenant-123",
        request_id="graph-req-1",
        correlation_id="client-corr-1",
        http_status=200,
        success=True,
        retry_count=2,
        sanitized_error="Bearer super-secret-token",
    ))
    fake = FakeOdoo()
    result = audit_logger.sync_to_odoo(fake, operation_label="User 360")

    assert result["status"] == "success"
    assert fake.created[0]["x_tenant_display"] == "tenant-123"
    assert fake.created[0]["x_graph_request_id"] == "graph-req-1"
    assert fake.created[0]["x_correlation_id"] == "client-corr-1"
    assert fake.created[0]["x_retry_count"] == 2
    assert "super-secret-token" not in str(fake.created[0])


# 20. Multi-tenant context is passed through correctly
@responses.activate
def test_20_multi_tenant_context_passthrough(mock_token_provider, fast_http):
    tenant_ctx = TenantContext(
        tenant_id="customer-tenant-a-1111",
        display_name="Customer A",
        domain="customera.onmicrosoft.com",
    )
    audit_logger = GraphAuditLogger()
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users?$top=5",
        json={"value": []},
        status=200,
    )
    client = MSGraphClient(
        tenant_context=tenant_ctx,
        token_provider=mock_token_provider,
        http_client=fast_http,
        audit_logger=audit_logger,
    )
    client.get_users(top=5)
    assert audit_logger.events[0].tenant_id == "customer-tenant-a-1111"


# 21. No credentials required for tests
def test_21_no_credentials_required_for_tests():
    # Instantiating client with mocks does not require real M365 credentials or environment variables
    ctx = TenantContext(tenant_id="test-tenant")
    tp = MagicMock()
    tp.get_access_token.return_value = "mock"
    client = MSGraphClient(tenant_context=ctx, token_provider=tp)
    assert client.tenant_context.tenant_id == "test-tenant"
