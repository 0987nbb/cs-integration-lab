# -*- coding: utf-8 -*-
"""Mocked unit tests for Synthetic Employee Onboarding Service."""
import re
from unittest.mock import MagicMock
import pytest
import responses

from integration_service.clients.ms_graph_client import MSGraphClient
from integration_service.clients.tenant_context import TenantContext
from integration_service.config import M365Settings
from integration_service.http_client import HttpSettings, ResilientHttpClient
from integration_service.onboarding import OnboardingService


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
def test_onboarding_plan_dry_run(m365_settings, mock_token_provider):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/organization",
        json={"value": [{"id": "t-1", "displayName": "Demo", "verifiedDomains": [{"name": "contoso.com", "isDefault": True}]}]},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/subscribedSkus",
        json={"value": [{"skuId": "s-1", "skuPartNumber": "SPE_E5", "consumedUnits": 5, "prepaidUnits": {"enabled": 10}}]},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users?$top=1&$select=userPrincipalName",
        json={"value": [{"userPrincipalName": "manager@contoso.com"}]},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users?$top=1&$select=userPrincipalName",
        json={"value": [{"userPrincipalName": "manager@contoso.com"}]},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/groups",
        json={"value": [{"id": "g-1", "displayName": "LAB-Engineers"}, {"id": "g-2", "displayName": "LAB-Helpdesk"}]},
        status=200,
    )

    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=ResilientHttpClient(settings=HttpSettings(max_retries=1)),
    )
    service = OnboardingService(graph_client=client)

    plan = service.plan_onboarding(first_name="Jane", last_name="Doe", job_title="Engineer")
    assert plan.upn == "lab-user-jane-doe@contoso.com"
    assert plan.target_sku_id == "s-1"
    assert plan.manager_upn == "manager@contoso.com"
    assert len(plan.target_group_ids) == 2
    assert len(plan.planned_mutations) == 5
    assert plan.validation_errors == []
    assert all(call.request.method == "GET" for call in responses.calls)


@responses.activate
def test_onboarding_execute_and_verify(m365_settings, mock_token_provider):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/organization",
        json={"value": [{"id": "t-1", "displayName": "Demo", "verifiedDomains": [{"name": "contoso.com", "isDefault": True}]}]},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/subscribedSkus",
        json={"value": [{"skuId": "s-1", "skuPartNumber": "SPE_E5", "consumedUnits": 5, "prepaidUnits": {"enabled": 10}}]},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/groups",
        json={"value": [{"id": "g-1", "displayName": "LAB-Engineers"}, {"id": "g-2", "displayName": "LAB-Helpdesk"}]},
        status=200,
    )
    # Check exists -> 404 (does not exist yet)
    responses.add(responses.GET, re.compile(r"https://graph\.microsoft\.com/v1\.0/users/lab-user-jane-doe@contoso\.com.*"), status=404)
    # Create user -> 201
    responses.add(responses.POST, "https://graph.microsoft.com/v1.0/users", json={"id": "u-new-123"}, status=201)
    # Assign license -> 200
    responses.add(responses.POST, "https://graph.microsoft.com/v1.0/users/u-new-123/assignLicense", json={}, status=200)
    # Resolve and assign manager
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users/manager@contoso.com?$select=id",
        json={"id": "manager-1"},
        status=200,
    )
    responses.add(responses.PUT, "https://graph.microsoft.com/v1.0/users/u-new-123/manager/$ref", status=204)
    # Add to two groups
    responses.add(responses.POST, "https://graph.microsoft.com/v1.0/groups/g-1/members/$ref", json={}, status=204)
    responses.add(responses.POST, "https://graph.microsoft.com/v1.0/groups/g-2/members/$ref", json={}, status=204)
    # Read-back verify user -> 200
    responses.add(
        responses.GET,
        re.compile(r"https://graph\.microsoft\.com/v1\.0/users/u-new-123\?.*"),
        json={"id": "u-new-123", "userPrincipalName": "lab-user-jane-doe@contoso.com", "accountEnabled": True, "jobTitle": "Engineer", "department": "Engineering", "usageLocation": "US", "assignedLicenses": [{"skuId": "s-1"}]},
        status=200,
    )
    # Read-back verify groups -> 200
    responses.add(responses.GET, "https://graph.microsoft.com/v1.0/users/u-new-123/memberOf", json={"value": [{"id": "g-1"}, {"id": "g-2"}]}, status=200)
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users/u-new-123/manager?$select=id,userPrincipalName",
        json={"id": "manager-1", "userPrincipalName": "manager@contoso.com"},
        status=200,
    )

    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=ResilientHttpClient(settings=HttpSettings(max_retries=1)),
    )
    service = OnboardingService(graph_client=client)

    plan = service.plan_onboarding(first_name="Jane", last_name="Doe", job_title="Engineer", manager_upn="manager@contoso.com")
    plan.upn = "lab-user-jane-doe@contoso.com"
    plan.target_sku_id = "s-1"
    plan.target_group_ids = ["g-1", "g-2"]
    plan.target_group_names = ["LAB-Engineers", "LAB-Helpdesk"]

    res = service.execute_onboarding(plan, approved=True)
    assert res.verification_passed is True
    assert res.graph_id == "u-new-123"
    assert res.stage == "verified"
    assert res.verified_properties["manager_id"] == "manager-1"


@responses.activate
def test_duplicate_onboarding_execution_repairs_by_readback_without_duplicate_mutations(m365_settings, mock_token_provider):
    responses.add(
        responses.GET,
        re.compile(r"https://graph\.microsoft\.com/v1\.0/users/lab-user-jane-doe@contoso\.com\?.*"),
        json={
            "id": "u-existing-123",
            "userPrincipalName": "lab-user-jane-doe@contoso.com",
            "displayName": "LAB-User Jane Doe",
            "accountEnabled": True,
            "jobTitle": "Engineer",
            "department": "Engineering",
            "usageLocation": "US",
            "assignedLicenses": [{"skuId": "s-1"}],
        },
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users/u-existing-123/memberOf",
        json={"value": [{"id": "g-1"}, {"id": "g-2"}]},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users/u-existing-123/manager?$select=id,userPrincipalName",
        json={"id": "manager-1", "userPrincipalName": "manager@contoso.com"},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users/manager@contoso.com?$select=id",
        json={"id": "manager-1"},
        status=200,
    )
    responses.add(
        responses.GET,
        re.compile(r"https://graph\.microsoft\.com/v1\.0/users/u-existing-123\?.*"),
        json={
            "id": "u-existing-123",
            "userPrincipalName": "lab-user-jane-doe@contoso.com",
            "displayName": "LAB-User Jane Doe",
            "accountEnabled": True,
            "jobTitle": "Engineer",
            "department": "Engineering",
            "usageLocation": "US",
            "assignedLicenses": [{"skuId": "s-1"}],
        },
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users/u-existing-123/memberOf",
        json={"value": [{"id": "g-1"}, {"id": "g-2"}]},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users/u-existing-123/manager?$select=id,userPrincipalName",
        json={"id": "manager-1", "userPrincipalName": "manager@contoso.com"},
        status=200,
    )

    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=ResilientHttpClient(settings=HttpSettings(max_retries=1)),
    )
    service = OnboardingService(graph_client=client)
    plan = service.plan_from_dict({
        "upn": "lab-user-jane-doe@contoso.com",
        "display_name": "LAB-User Jane Doe",
        "job_title": "Engineer",
        "department": "Engineering",
        "usage_location": "US",
        "manager_upn": "manager@contoso.com",
        "target_sku_id": "s-1",
        "target_sku_part_number": "SPE_E5",
        "target_group_ids": ["g-1", "g-2"],
        "target_group_names": ["LAB-Engineers", "LAB-Helpdesk"],
        "planned_mutations": [],
        "validation_errors": [],
    })

    res = service.execute_onboarding(plan, approved=True)
    assert res.verification_passed is True
    assert res.stage == "verified"
    mutation_methods = [call.request.method for call in responses.calls if call.request.method in {"POST", "PATCH", "PUT", "DELETE"}]
    assert mutation_methods == []


@responses.activate
def test_onboarding_failed_readback_verification(m365_settings, mock_token_provider):
    responses.add(responses.GET, re.compile(r"https://graph\.microsoft\.com/v1\.0/users/lab-user-jane-doe@contoso\.com.*"), status=404)
    responses.add(responses.POST, "https://graph.microsoft.com/v1.0/users", json={"id": "u-new-123"}, status=201)
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users/manager@contoso.com?$select=id",
        json={"id": "manager-1"},
        status=200,
    )
    responses.add(responses.PUT, "https://graph.microsoft.com/v1.0/users/u-new-123/manager/$ref", status=204)
    responses.add(responses.POST, "https://graph.microsoft.com/v1.0/users/u-new-123/assignLicense", json={}, status=200)
    responses.add(responses.POST, "https://graph.microsoft.com/v1.0/groups/g-1/members/$ref", json={}, status=204)
    responses.add(responses.POST, "https://graph.microsoft.com/v1.0/groups/g-2/members/$ref", json={}, status=204)
    responses.add(
        responses.GET,
        re.compile(r"https://graph\.microsoft\.com/v1\.0/users/u-new-123\?.*"),
        json={
            "id": "u-new-123",
            "userPrincipalName": "lab-user-jane-doe@contoso.com",
            "accountEnabled": True,
            "jobTitle": "Engineer",
            "department": "Engineering",
            "usageLocation": "US",
            "assignedLicenses": [],
        },
        status=200,
    )
    responses.add(responses.GET, "https://graph.microsoft.com/v1.0/users/u-new-123/memberOf", json={"value": [{"id": "g-1"}, {"id": "g-2"}]}, status=200)
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users/u-new-123/manager?$select=id,userPrincipalName",
        json={"id": "manager-1", "userPrincipalName": "manager@contoso.com"},
        status=200,
    )

    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=ResilientHttpClient(settings=HttpSettings(max_retries=1)),
    )
    service = OnboardingService(graph_client=client)
    plan = service.plan_from_dict({
        "upn": "lab-user-jane-doe@contoso.com",
        "display_name": "LAB-User Jane Doe",
        "job_title": "Engineer",
        "department": "Engineering",
        "usage_location": "US",
        "manager_upn": "manager@contoso.com",
        "target_sku_id": "s-1",
        "target_sku_part_number": "SPE_E5",
        "target_group_ids": ["g-1", "g-2"],
        "target_group_names": ["LAB-Engineers", "LAB-Helpdesk"],
        "planned_mutations": [],
        "validation_errors": [],
    })

    res = service.execute_onboarding(plan, approved=True)
    assert res.stage == "failed"
    assert res.verification_passed is False
    assert res.verified_properties["assigned_license_count"] == 0


@responses.activate
def test_partial_onboarding_failure_after_user_creation(m365_settings, mock_token_provider):
    responses.add(responses.GET, re.compile(r"https://graph\.microsoft\.com/v1\.0/users/lab-user-jane-doe@contoso\.com.*"), status=404)
    responses.add(responses.POST, "https://graph.microsoft.com/v1.0/users", json={"id": "u-new-123"}, status=201)
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users/manager@contoso.com?$select=id",
        json={"id": "manager-1"},
        status=200,
    )
    responses.add(responses.PUT, "https://graph.microsoft.com/v1.0/users/u-new-123/manager/$ref", status=204)
    responses.add(
        responses.POST,
        "https://graph.microsoft.com/v1.0/users/u-new-123/assignLicense",
        json={"error": {"code": "LicenseAssignmentFailed"}},
        status=400,
    )
    responses.add(responses.POST, "https://graph.microsoft.com/v1.0/groups/g-1/members/$ref", json={}, status=204)
    responses.add(responses.POST, "https://graph.microsoft.com/v1.0/groups/g-2/members/$ref", json={}, status=204)
    responses.add(
        responses.GET,
        re.compile(r"https://graph\.microsoft\.com/v1\.0/users/u-new-123\?.*"),
        json={
            "id": "u-new-123",
            "userPrincipalName": "lab-user-jane-doe@contoso.com",
            "accountEnabled": True,
            "jobTitle": "Engineer",
            "department": "Engineering",
            "usageLocation": "US",
            "assignedLicenses": [],
        },
        status=200,
    )
    responses.add(responses.GET, "https://graph.microsoft.com/v1.0/users/u-new-123/memberOf", json={"value": [{"id": "g-1"}, {"id": "g-2"}]}, status=200)
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/users/u-new-123/manager?$select=id,userPrincipalName",
        json={"id": "manager-1", "userPrincipalName": "manager@contoso.com"},
        status=200,
    )

    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=ResilientHttpClient(settings=HttpSettings(max_retries=1)),
    )
    service = OnboardingService(graph_client=client)
    plan = service.plan_from_dict({
        "upn": "lab-user-jane-doe@contoso.com",
        "display_name": "LAB-User Jane Doe",
        "job_title": "Engineer",
        "department": "Engineering",
        "usage_location": "US",
        "manager_upn": "manager@contoso.com",
        "target_sku_id": "s-1",
        "target_sku_part_number": "SPE_E5",
        "target_group_ids": ["g-1", "g-2"],
        "target_group_names": ["LAB-Engineers", "LAB-Helpdesk"],
        "planned_mutations": [],
        "validation_errors": [],
    })

    res = service.execute_onboarding(plan, approved=True)
    assert res.stage == "failed"
    assert res.graph_id == "u-new-123"
    assert res.verification_passed is False
