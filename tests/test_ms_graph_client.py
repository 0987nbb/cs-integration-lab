# -*- coding: utf-8 -*-
"""Unit tests for MSGraphClient (Microsoft Graph API client).

Verifies token acquisition, token caching/redaction, user querying, 401/403
error handling, timeout resilience, and credential masking.
All tests run fully offline with mocks/responses.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests
import responses

from integration_service.clients.ms_graph_client import MSGraphClient
from integration_service.config import M365Settings
from integration_service.errors import ClientError, ConfigurationError, TimeoutError_
from integration_service.http_client import HttpSettings, ResilientHttpClient
from integration_service.sanitize import clear_secrets, sanitize


@pytest.fixture
def m365_settings() -> M365Settings:
    return M365Settings(
        tenant_id="00000000-0000-0000-0000-000000000000",
        client_id="11111111-1111-1111-1111-111111111111",
        client_secret="test-super-secret-m365-client-secret-999",
        graph_base_url="https://graph.microsoft.com/v1.0",
    )


@pytest.fixture
def mock_msal_app():
    mock_app = MagicMock()
    mock_app.acquire_token_for_client.return_value = {
        "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.mock-graph-access-token-val-00001"
    }
    return mock_app


@pytest.fixture
def fast_http() -> ResilientHttpClient:
    settings = HttpSettings(max_retries=1, backoff_factor=0.01, backoff_max=0.05)
    return ResilientHttpClient(settings=settings, jitter=False)


def test_token_acquisition_success(m365_settings, mock_msal_app, fast_http):
    client = MSGraphClient(
        settings=m365_settings,
        http_client=fast_http,
        msal_app=mock_msal_app,
    )
    token = client.get_access_token()
    assert token == "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.mock-graph-access-token-val-00001"
    mock_msal_app.acquire_token_for_client.assert_called_once_with(
        scopes=["https://graph.microsoft.com/.default"]
    )
    # Verify token is redacted by sanitize
    assert sanitize(token) == "[REDACTED]"


def test_token_acquisition_failure(m365_settings, fast_http):
    mock_app = MagicMock()
    mock_app.acquire_token_for_client.return_value = {
        "error": "invalid_client",
        "error_description": "AADSTS7000215: Invalid client secret provided for test-super-secret-m365-client-secret-999",
    }

    client = MSGraphClient(
        settings=m365_settings,
        http_client=fast_http,
        msal_app=mock_app,
    )

    with pytest.raises(ClientError) as exc_info:
        client.get_access_token()

    assert exc_info.value.status_code == 401
    # Secret must be redacted in the exception message
    assert "test-super-secret-m365-client-secret-999" not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)


def test_unconfigured_client_raises_configuration_error(fast_http):
    empty_settings = M365Settings()
    client = MSGraphClient(settings=empty_settings, http_client=fast_http)

    with pytest.raises(ConfigurationError) as exc_info:
        client.get_access_token()

    assert "Missing required Microsoft Graph settings" in str(exc_info.value)


@responses.activate
def test_get_users_success(m365_settings, mock_msal_app, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users?$top=5",
        json={
            "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#users",
            "value": [
                {
                    "id": "user-id-001",
                    "displayName": "Jane Doe",
                    "userPrincipalName": "jane.doe@example.com",
                    "mail": "jane.doe@example.com",
                    "jobTitle": "Systems Engineer",
                },
                {
                    "id": "user-id-002",
                    "displayName": "John Smith",
                    "userPrincipalName": "john.smith@example.com",
                    "mail": "john.smith@example.com",
                    "jobTitle": "Cloud Architect",
                },
            ],
        },
        status=200,
    )

    client = MSGraphClient(
        settings=m365_settings,
        http_client=fast_http,
        msal_app=mock_msal_app,
    )

    result = client.get_users(top=5)
    assert "value" in result
    users = result["value"]
    assert len(users) == 2
    assert users[0]["displayName"] == "Jane Doe"
    assert users[1]["displayName"] == "John Smith"

    # Verify authorization header had Bearer token
    assert responses.calls[0].request.headers["Authorization"].startswith("Bearer eyJhbGci")


@responses.activate
def test_get_users_401_403_handling(m365_settings, mock_msal_app, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users?$top=5",
        json={
            "error": {
                "code": "Authorization_RequestDenied",
                "message": "Insufficient privileges to complete the operation.",
            }
        },
        status=403,
    )

    client = MSGraphClient(
        settings=m365_settings,
        http_client=fast_http,
        msal_app=mock_msal_app,
    )

    with pytest.raises(ClientError) as exc_info:
        client.get_users(top=5)

    assert exc_info.value.status_code == 403
    assert "403" in str(exc_info.value)


@responses.activate
def test_get_users_timeout_handling(m365_settings, mock_msal_app, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users?$top=5",
        body=requests.exceptions.Timeout("Connection timed out"),
    )

    client = MSGraphClient(
        settings=m365_settings,
        http_client=fast_http,
        msal_app=mock_msal_app,
    )

    with pytest.raises(TimeoutError_):
        client.get_users(top=5)


def test_secret_and_token_redaction(m365_settings, mock_msal_app, fast_http):
    client = MSGraphClient(
        settings=m365_settings,
        http_client=fast_http,
        msal_app=mock_msal_app,
    )

    secret = m365_settings.client_secret
    token = client.get_access_token()

    log_line_secret = f"Connecting with client secret: {secret}"
    log_line_token = f"Authorization header: Bearer {token}"

    assert secret not in sanitize(log_line_secret)
    assert token not in sanitize(log_line_token)
    assert "[REDACTED]" in sanitize(log_line_secret)
    assert "[REDACTED]" in sanitize(log_line_token)
