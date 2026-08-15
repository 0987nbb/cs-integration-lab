# -*- coding: utf-8 -*-
"""Mocked unit tests for Synthetic Employee Offboarding Service."""
import re
from unittest.mock import MagicMock
import pytest
import responses

from integration_service.clients.ms_graph_client import MSGraphClient
from integration_service.clients.tenant_context import TenantContext
from integration_service.config import M365Settings
from integration_service.http_client import HttpSettings, ResilientHttpClient
from integration_service.offboarding import OffboardingService


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


@responses.activate
def test_offboarding_execute_and_verify(m365_settings, mock_token_provider):
    # Pre-state -> accountEnabled=True, assignedLicenses=[sku-1], groups=[g-removable]
    responses.add(
        responses.GET,
        re.compile(r"https://graph\.microsoft\.com/v1\.0/users/lab-worker@contoso\.com\?.*"),
        json={"id": "u-off-99", "userPrincipalName": "lab-worker@contoso.com", "accountEnabled": True, "assignedLicenses": [{"skuId": "sku-1"}]},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users/lab-worker@contoso.com/memberOf",
        json={"value": [{"id": "g-removable", "displayName": "LAB-Removable"}]},
        status=200,
    )

    # 1. Revoke sessions
    responses.add(responses.POST, "https://graph.microsoft.com/v1.0/users/u-off-99/revokeSignInSessions", status=200)
    # 2. Disable signin
    responses.add(responses.PATCH, "https://graph.microsoft.com/v1.0/users/u-off-99", status=204)
    # 3. Remove group
    responses.add(responses.DELETE, "https://graph.microsoft.com/v1.0/groups/g-removable/members/u-off-99/$ref", status=204)
    # 4. Remove license
    responses.add(responses.POST, "https://graph.microsoft.com/v1.0/users/u-off-99/assignLicense", json={}, status=200)

    # Post-state -> accountEnabled=False, assignedLicenses=[], groups=[]
    responses.add(
        responses.GET,
        re.compile(r"https://graph\.microsoft\.com/v1\.0/users/u-off-99\?.*"),
        json={"id": "u-off-99", "userPrincipalName": "lab-worker@contoso.com", "accountEnabled": False, "assignedLicenses": []},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users/u-off-99/memberOf",
        json={"value": []},
        status=200,
    )

    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=ResilientHttpClient(settings=HttpSettings(max_retries=1)),
    )
    service = OffboardingService(graph_client=client)

    res = service.execute_offboarding("lab-worker@contoso.com", dry_run=False, approved=True)
    assert res.verification_passed is True
    assert res.signin_disabled is True
    assert res.status == "success"
    assert "g-removable" in res.removed_group_ids


@responses.activate
def test_offboarding_preserves_configured_groups_and_licenses(m365_settings, mock_token_provider):
    responses.add(
        responses.GET,
        re.compile(r"https://graph\.microsoft\.com/v1\.0/users/lab-worker@contoso\.com\?.*"),
        json={
            "id": "u-off-99",
            "userPrincipalName": "lab-worker@contoso.com",
            "accountEnabled": True,
            "assignedLicenses": [{"skuId": "sku-remove"}, {"skuId": "sku-preserve"}],
        },
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users/lab-worker@contoso.com/memberOf",
        json={"value": [
            {"id": "g-remove", "displayName": "LAB-Remove"},
            {"id": "g-protect", "displayName": "LAB-Protected"},
            {"id": "g-real", "displayName": "Real Department"},
        ]},
        status=200,
    )
    responses.add(responses.POST, "https://graph.microsoft.com/v1.0/users/u-off-99/revokeSignInSessions", status=200)
    responses.add(responses.PATCH, "https://graph.microsoft.com/v1.0/users/u-off-99", status=204)
    responses.add(responses.DELETE, "https://graph.microsoft.com/v1.0/groups/g-remove/members/u-off-99/$ref", status=204)
    responses.add(responses.POST, "https://graph.microsoft.com/v1.0/users/u-off-99/assignLicense", json={}, status=200)
    responses.add(
        responses.GET,
        re.compile(r"https://graph\.microsoft\.com/v1\.0/users/u-off-99\?.*"),
        json={
            "id": "u-off-99",
            "userPrincipalName": "lab-worker@contoso.com",
            "accountEnabled": False,
            "assignedLicenses": [{"skuId": "sku-preserve"}],
        },
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users/u-off-99/memberOf",
        json={"value": [
            {"id": "g-protect", "displayName": "LAB-Protected"},
            {"id": "g-real", "displayName": "Real Department"},
        ]},
        status=200,
    )

    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=ResilientHttpClient(settings=HttpSettings(max_retries=1)),
    )
    service = OffboardingService(
        graph_client=client,
        protected_group_ids={"g-protect"},
        removable_sku_ids={"sku-remove"},
    )

    res = service.execute_offboarding("lab-worker@contoso.com", dry_run=False, approved=True)
    assert res.verification_passed is True
    assert res.removed_group_ids == ["g-remove"]
    assert set(res.preserved_group_ids) == {"g-protect", "g-real"}
    assert res.removed_sku_ids == ["sku-remove"]
    assert res.preserved_sku_ids == ["sku-preserve"]
    assert res.verification_report["preserved_groups_present"] is True
    assert res.verification_report["preserved_skus_present"] is True


@responses.activate
def test_offboarding_rerun_skips_unnecessary_mutations(m365_settings, mock_token_provider):
    responses.add(
        responses.GET,
        re.compile(r"https://graph\.microsoft\.com/v1\.0/users/lab-worker@contoso\.com\?.*"),
        json={"id": "u-off-99", "userPrincipalName": "lab-worker@contoso.com", "accountEnabled": False, "assignedLicenses": []},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users/lab-worker@contoso.com/memberOf",
        json={"value": []},
        status=200,
    )
    responses.add(
        responses.GET,
        re.compile(r"https://graph\.microsoft\.com/v1\.0/users/u-off-99\?.*"),
        json={"id": "u-off-99", "userPrincipalName": "lab-worker@contoso.com", "accountEnabled": False, "assignedLicenses": []},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users/u-off-99/memberOf",
        json={"value": []},
        status=200,
    )

    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=ResilientHttpClient(settings=HttpSettings(max_retries=1)),
    )
    service = OffboardingService(graph_client=client)

    res = service.execute_offboarding("lab-worker@contoso.com", dry_run=False, approved=True)
    assert res.verification_passed is True
    assert res.verification_report["sessions_revoke_skipped"] is True
    methods = [call.request.method for call in responses.calls]
    assert "POST" not in methods
    assert "PATCH" not in methods
    assert "DELETE" not in methods
