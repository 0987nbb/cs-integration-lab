# -*- coding: utf-8 -*-
"""Mocked unit tests for User 360 Diagnostic Service."""
from unittest.mock import MagicMock
import pytest
import responses

from integration_service.clients.ms_graph_client import MSGraphClient
from integration_service.clients.tenant_context import TenantContext
from integration_service.config import M365Settings
from integration_service.http_client import HttpSettings, ResilientHttpClient
from integration_service.user_360 import User360Service, compare_snapshots


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
def test_user_360_diagnostic_success(m365_settings, mock_token_provider):
    # 1. Profile
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users/admin@contoso.com?$select=id,userPrincipalName,displayName,accountEnabled,jobTitle,department,usageLocation,assignedLicenses,assignedPlans",
        json={
            "id": "u-100",
            "userPrincipalName": "admin@contoso.com",
            "displayName": "Admin User",
            "accountEnabled": True,
            "jobTitle": "IT Administrator",
            "department": "IT",
            "usageLocation": "US",
            "assignedLicenses": [{"skuId": "sku-e5"}],
            "assignedPlans": [],
        },
        status=200,
    )
    # 2. Manager
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users/u-100/manager",
        json={"id": "m-200", "displayName": "Executive Director", "userPrincipalName": "dir@contoso.com"},
        status=200,
    )
    # 3. Direct Groups
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users/u-100/memberOf",
        json={"value": [{"@odata.type": "#microsoft.graph.group", "id": "g-1", "displayName": "LAB-Admins"}]},
        status=200,
    )
    # 4. Transitive Groups
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users/u-100/transitiveMemberOf",
        json={"value": [{"@odata.type": "#microsoft.graph.group", "id": "g-2", "displayName": "All-Employees"}]},
        status=200,
    )
    # 5. Auth methods & Devices 404/403 (Graceful degradation)
    responses.add(responses.GET, "https://graph.microsoft.com/v1.0/users/u-100/authentication/methods", status=404)
    responses.add(responses.GET, "https://graph.microsoft.com/v1.0/users/u-100/registeredDevices", status=403)
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/deviceManagement/managedDevices?$filter=userPrincipalName+eq+%27admin%40contoso.com%27",
        status=403,
    )

    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=ResilientHttpClient(settings=HttpSettings(max_retries=1)),
    )
    service = User360Service(graph_client=client)

    snapshot = service.get_user_snapshot("admin@contoso.com")
    assert snapshot.upn == "admin@contoso.com"
    assert snapshot.graph_id == "u-100"
    assert snapshot.account_enabled is True
    assert snapshot.job_title == "IT Administrator"
    assert snapshot.manager.get("displayName") == "Executive Director"
    assert len(snapshot.direct_groups) == 1
    assert snapshot.direct_groups[0]["displayName"] == "LAB-Admins"
    assert snapshot.auth_methods == "Not available"
    assert snapshot.devices["registered"] == "Not available"
    assert snapshot.devices["managed"] == "Not available"
    assert snapshot.availability["auth_methods"] == "Not available"
    assert snapshot.availability["registered_devices"] == "Not available - permission denied"
    assert snapshot.availability["managed_devices"] == "Not available - permission denied"
    assert snapshot.diagnostic_status == "partial"
    assert "registered_devices" in snapshot.error_details

    for call in responses.calls:
        assert call.request.method == "GET"


def test_compare_snapshots_includes_transitive_group_changes():
    snap1 = {
        "upn": "admin@contoso.com",
        "snapshot_timestamp": "2026-08-15 10:00:00",
        "x_group_memberships": '{"direct": [{"displayName": "LAB-Admins"}], "transitive": []}',
    }
    snap2 = {
        "upn": "admin@contoso.com",
        "snapshot_timestamp": "2026-08-15 11:00:00",
        "x_group_memberships": '{"direct": [{"displayName": "LAB-Admins"}], "transitive": [{"displayName": "All Employees"}]}',
    }

    diff = compare_snapshots(snap1, snap2)

    assert diff["added_groups"] == ["All Employees"]
