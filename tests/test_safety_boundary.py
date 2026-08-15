# -*- coding: utf-8 -*-
"""Unit tests verifying safety boundary enforcement (LAB-* only, target validation)."""
from unittest.mock import MagicMock
import pytest

from integration_service.clients.ms_graph_client import MSGraphClient
from integration_service.clients.tenant_context import TenantContext
from integration_service.config import M365Settings
from integration_service.errors import InvalidPayloadError
from integration_service.offboarding import OffboardingService
from integration_service.onboarding import OnboardingPlan, OnboardingService
from integration_service.remediation import RemediationService


@pytest.fixture
def m365_settings() -> M365Settings:
    return M365Settings(
        tenant_id="test-tenant-123",
        client_id="test-client-123",
        client_secret="test-secret-123",
        graph_base_url="https://graph.microsoft.com/v1.0",
    )


@pytest.fixture
def mock_token_provider() -> MagicMock:
    provider = MagicMock()
    provider.get_access_token.return_value = "mock-token-123"
    return provider


def test_remediation_rejects_non_lab_user(m365_settings, mock_token_provider):
    mock_http = MagicMock()
    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=mock_http,
    )
    service = RemediationService(graph_client=client)

    # Rejects real admin user when approved=True
    with pytest.raises(InvalidPayloadError, match="Safety Violation"):
        service.execute_remediation(action="block_signin", upn_or_id="admin@crmbc388484.onmicrosoft.com", approved=True, dry_run=False)

    # Zero HTTP requests sent to Graph API
    assert mock_http.request.call_count == 0


def test_offboarding_rejects_non_lab_user(m365_settings, mock_token_provider):
    mock_http = MagicMock()
    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=mock_http,
    )
    service = OffboardingService(graph_client=client)

    # Rejects real user when dry_run=False
    with pytest.raises(InvalidPayloadError, match="Safety Violation"):
        service.execute_offboarding(upn_or_id="admin@crmbc388484.onmicrosoft.com", dry_run=False)

    # Zero HTTP requests sent to Graph API
    assert mock_http.request.call_count == 0


def test_onboarding_fails_on_zero_license_capacity(m365_settings, mock_token_provider):
    mock_http = MagicMock()
    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=mock_http,
    )
    service = OnboardingService(graph_client=client)

    plan = OnboardingPlan(
        upn="lab-user-test@contoso.com",
        display_name="LAB-User Test",
        job_title="Dev",
        department="IT",
        usage_location="US",
        manager_upn=None,
        target_sku_id=None,  # No capacity available
        target_sku_part_number=None,
    )

    result = service.execute_onboarding(plan)
    assert result.stage == "failed"
    assert "Insufficient license capacity" in result.error_details
