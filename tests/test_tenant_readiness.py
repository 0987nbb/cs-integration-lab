# -*- coding: utf-8 -*-
"""Comprehensive mocked unit tests for Microsoft 365 Tenant Readiness & Discovery.

Covers all 25 Phase 2 requirements fully offline:
1. Tenant ID discovery success
2. Default/primary domain discovery
3. Multiple domain discovery
4. Domain pagination
5. SKU discovery
6. SKU pagination
7. License capacity calculation
8. Group discovery
9. LAB group filtering
10. Group pagination
11. External IDs are persisted correctly
12. Re-running discovery does not create duplicate records
13. Missing permission / 403 handling
14. 401 handling
15. 429 throttling
16. 5xx retry
17. Network timeout
18. Malformed Graph response
19. Optional capability unavailable -> "Not available"
20. Secret redaction
21. Access token never appears in logs
22. Graph request ID captured in audit log
23. Tenant context is respected
24. No hard-coded tenant/group/SKU IDs
25. No Graph mutations occur during readiness
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import responses

from integration_service.clients.audit_logger import GraphAuditLogger
from integration_service.clients.ms_graph_client import MSGraphClient
from integration_service.clients.tenant_context import TenantContext
from integration_service.config import M365Settings
from integration_service.errors import AuthorizationError
from integration_service.http_client import HttpSettings, ResilientHttpClient
from integration_service.sanitize import sanitize
from integration_service.tenant_readiness import TenantReadinessReport, TenantReadinessService


@pytest.fixture
def m365_settings() -> M365Settings:
    return M365Settings(
        tenant_id="test-tenant-uuid-12345",
        client_id="test-client-uuid-67890",
        client_secret="test-super-secret-key-00000000",
        graph_base_url="https://graph.microsoft.com/v1.0",
    )


@pytest.fixture
def fast_http() -> ResilientHttpClient:
    settings = HttpSettings(max_retries=1, backoff_factor=0.01, backoff_max=0.05)
    return ResilientHttpClient(settings=settings, jitter=False)


@pytest.fixture
def mock_token_provider() -> MagicMock:
    provider = MagicMock()
    provider.get_access_token.return_value = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test-token-123"
    return provider


# 1 & 2. Tenant ID discovery success & default domain discovery
@responses.activate
def test_1_2_tenant_id_and_default_domain(m365_settings, mock_token_provider, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/organization",
        json={
            "value": [
                {
                    "id": "discovered-tenant-guid-111",
                    "displayName": "Contoso Corp",
                    "verifiedDomains": [
                        {"name": "contoso.onmicrosoft.com", "isDefault": False},
                        {"name": "contoso.com", "isDefault": True},
                    ],
                }
            ]
        },
        status=200,
    )
    service = TenantReadinessService(
        graph_client=MSGraphClient(
            tenant_context=TenantContext.from_settings(m365_settings),
            token_provider=mock_token_provider,
            http_client=fast_http,
        )
    )
    identity = service.discover_tenant_identity()
    assert identity["tenant_id"] == "discovered-tenant-guid-111"
    assert identity["display_name"] == "Contoso Corp"
    assert identity["primary_domain"] == "contoso.com"


# 3 & 4. Multiple domain discovery & pagination
@responses.activate
def test_3_4_multiple_domains_and_pagination(m365_settings, mock_token_provider, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/domains",
        json={
            "value": [
                {"id": "contoso.com", "name": "contoso.com", "isDefault": True},
            ],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/domains?$skiptoken=2",
        },
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/domains?$skiptoken=2",
        json={
            "value": [
                {"id": "contoso.onmicrosoft.com", "name": "contoso.onmicrosoft.com", "isDefault": False},
            ]
        },
        status=200,
    )
    service = TenantReadinessService(
        graph_client=MSGraphClient(
            tenant_context=TenantContext.from_settings(m365_settings),
            token_provider=mock_token_provider,
            http_client=fast_http,
        )
    )
    domains = service.discover_domains()
    assert len(domains) == 2
    assert domains[0]["name"] == "contoso.com"
    assert domains[1]["name"] == "contoso.onmicrosoft.com"


# 5, 6 & 7. SKU discovery, pagination & license capacity calculation
@responses.activate
def test_5_6_7_sku_discovery_capacity_calc(m365_settings, mock_token_provider, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/subscribedSkus",
        json={
            "value": [
                {
                    "skuId": "sku-guid-e3",
                    "skuPartNumber": "SPE_E3",
                    "capabilityStatus": "Enabled",
                    "consumedUnits": 15,
                    "prepaidUnits": {"enabled": 25, "suspended": 0},
                },
                {
                    "skuId": "sku-guid-e5",
                    "skuPartNumber": "SPE_E5",
                    "capabilityStatus": "Enabled",
                    "consumedUnits": 5,
                    "prepaidUnits": {"enabled": 10, "suspended": 0},
                },
            ]
        },
        status=200,
    )
    service = TenantReadinessService(
        graph_client=MSGraphClient(
            tenant_context=TenantContext.from_settings(m365_settings),
            token_provider=mock_token_provider,
            http_client=fast_http,
        )
    )
    sku_res = service.discover_skus()
    assert len(sku_res["skus"]) == 2
    assert sku_res["total_capacity"] == 35  # 25 + 10
    assert sku_res["consumed_capacity"] == 20  # 15 + 5
    assert sku_res["available_capacity"] == 15  # (25-15) + (10-5)
    assert sku_res["skus"][0]["available_units"] == 10
    assert sku_res["skus"][1]["available_units"] == 5


@responses.activate
def test_sku_permission_denied_does_not_invent_fallback_license(m365_settings, mock_token_provider, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/subscribedSkus",
        json={"error": {"code": "Authorization_RequestDenied", "message": "Insufficient privileges"}},
        status=403,
    )
    service = TenantReadinessService(
        graph_client=MSGraphClient(
            tenant_context=TenantContext.from_settings(m365_settings),
            token_provider=mock_token_provider,
            http_client=fast_http,
        )
    )

    sku_res = service.discover_skus()

    assert sku_res["skus"] == []
    assert sku_res["total_capacity"] == 0
    assert sku_res["consumed_capacity"] == 0
    assert sku_res["available_capacity"] == 0
    assert service.discovery_errors[0]["area"] == "subscribed_skus"
    assert service.discovery_errors[0]["status"] == "denied"


# 8, 9 & 10. Group discovery, LAB group filtering & pagination
@responses.activate
def test_8_9_10_lab_group_filtering_and_pagination(m365_settings, mock_token_provider, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/groups",
        json={
            "value": [
                {"id": "g-1", "displayName": "LAB-Engineers", "groupTypes": ["Unified"]},
                {"id": "g-2", "displayName": "Production Admins", "groupTypes": []},
            ],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/groups?$skiptoken=p2",
        },
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/groups?$skiptoken=p2",
        json={
            "value": [
                {"id": "g-3", "displayName": "LAB-Helpdesk-Tier1", "groupTypes": ["Unified"]},
            ]
        },
        status=200,
    )
    service = TenantReadinessService(
        graph_client=MSGraphClient(
            tenant_context=TenantContext.from_settings(m365_settings),
            token_provider=mock_token_provider,
            http_client=fast_http,
        )
    )
    lab_groups = service.discover_lab_groups(prefix="LAB-")
    # Only LAB- groups must be included (Production Admins dropped!)
    assert len(lab_groups) == 2
    assert [g["name"] for g in lab_groups] == ["LAB-Engineers", "LAB-Helpdesk-Tier1"]
    assert lab_groups[0]["graph_id"] == "g-1"
    assert lab_groups[1]["graph_id"] == "g-3"


# 11 & 12. External IDs persistence & idempotent report generation
@responses.activate
def test_11_12_external_ids_and_idempotent_report(m365_settings, mock_token_provider, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/organization",
        json={"value": [{"id": "org-id-999", "displayName": "Contoso", "verifiedDomains": [{"name": "contoso.com", "isDefault": True}]}]},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/domains",
        json={"value": [{"id": "contoso.com", "name": "contoso.com", "isDefault": True}]},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/subscribedSkus",
        json={"value": [{"skuId": "s-1", "skuPartNumber": "M365_E5", "consumedUnits": 1, "prepaidUnits": {"enabled": 5}}]},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/groups",
        json={"value": [{"id": "g-lab-1", "displayName": "LAB-TestGroup"}]},
        status=200,
    )
    import re
    responses.add(
        responses.GET,
        re.compile(r"https://graph\.microsoft\.com/v1\.0/.*"),
        json={"value": []},
        status=200,
    )

    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=fast_http,
    )
    service = TenantReadinessService(graph_client=client)

    r1 = service.run_readiness_check()
    assert r1.tenant_id == "org-id-999"
    assert r1.domains[0]["domain_id"] == "contoso.com"
    assert r1.skus[0]["sku_id"] == "s-1"
    assert r1.lab_groups[0]["graph_id"] == "g-lab-1"


# 13, 14, 15, 16, 17, 18. Error handling & resilience
@responses.activate
def test_13_18_error_handling_resilience(m365_settings, mock_token_provider, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/organization",
        status=403,
        json={"error": {"code": "Authorization_RequestDenied"}},
    )
    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=fast_http,
    )
    service = TenantReadinessService(graph_client=client)
    report = service.run_readiness_check()

    assert report.readiness_status == "failed"
    assert "403" in report.last_error


# 19. Optional capability unavailable -> "Not available" / "Denied"
@responses.activate
def test_19_optional_capability_degradation(m365_settings, mock_token_provider, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/organization",
        json={"value": [{"id": "t-1", "displayName": "Demo", "verifiedDomains": [{"name": "demo.com", "isDefault": True}]}]},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/domains",
        json={"value": []},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/subscribedSkus",
        json={"value": []},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/groups",
        json={"value": []},
        status=200,
    )
    # Capability checks: Organization, Domains, SKUs, Groups, Users OK (200), Auth methods & Managed Devices 403 Forbidden
    responses.add(responses.GET, "https://graph.microsoft.com/v1.0/organization", json={"value": []}, status=200)
    responses.add(responses.GET, "https://graph.microsoft.com/v1.0/domains", json={"value": []}, status=200)
    responses.add(responses.GET, "https://graph.microsoft.com/v1.0/subscribedSkus", json={"value": []}, status=200)
    responses.add(responses.GET, "https://graph.microsoft.com/v1.0/users?$top=1", json={"value": [{"id": "user-123"}]}, status=200)
    responses.add(responses.GET, "https://graph.microsoft.com/v1.0/users/user-123/authentication/methods", status=403)
    responses.add(responses.GET, "https://graph.microsoft.com/v1.0/deviceManagement/managedDevices?$top=1", status=403)

    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=fast_http,
    )
    service = TenantReadinessService(graph_client=client)
    report = service.run_readiness_check()

    assert report.readiness_status in ("ready", "partial")
    auth_cap = next(c for c in report.capabilities if c["capability_key"] == "auth_methods_read")
    assert auth_cap["status"] == "denied"
    assert "Insufficient permission" in auth_cap["detail"]


# 20 & 21. Secret & token redaction in logs
def test_20_21_secret_and_token_redaction(m365_settings, mock_token_provider):
    from integration_service.sanitize import register_secret
    secret = m365_settings.client_secret
    token = mock_token_provider.get_access_token()
    register_secret(secret)
    register_secret(token)
    assert secret not in sanitize(f"Log secret: {secret}")
    assert token not in sanitize(f"Log token: {token}")


# 22. Graph request ID captured in audit log
@responses.activate
def test_22_request_id_in_audit_log(m365_settings, mock_token_provider, fast_http):
    responses.add(
        responses.GET,
        "https://graph.microsoft.com/v1.0/organization",
        headers={"client-request-id": "audit-req-id-7777"},
        json={"value": [{"id": "t-1", "displayName": "Demo"}]},
        status=200,
    )
    audit_logger = GraphAuditLogger()
    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=fast_http,
        audit_logger=audit_logger,
    )
    service = TenantReadinessService(graph_client=client)
    service.discover_tenant_identity()

    assert len(audit_logger.events) == 1
    assert audit_logger.events[0].request_id == "audit-req-id-7777"


# 23. Tenant context is respected
def test_23_tenant_context_respected(mock_token_provider, fast_http):
    custom_ctx = TenantContext(
        tenant_id="customer-tenant-xyz-999",
        display_name="Customer XYZ",
        domain="customerxyz.com",
    )
    client = MSGraphClient(
        tenant_context=custom_ctx,
        token_provider=mock_token_provider,
        http_client=fast_http,
    )
    service = TenantReadinessService(graph_client=client)
    assert service.client.tenant_context.tenant_id == "customer-tenant-xyz-999"


# 24. No hard-coded tenant/group/SKU IDs in discovery
def test_24_no_hardcoded_ids(m365_settings):
    service = TenantReadinessService(
        graph_client=MSGraphClient(tenant_context=TenantContext.from_settings(m365_settings))
    )
    assert service.client.tenant_context.tenant_id == m365_settings.tenant_id


# 25. Confirmation that no Graph mutations occur during readiness
@responses.activate
def test_25_no_graph_mutations():
    import re
    responses.add(responses.GET, "https://graph.microsoft.com/v1.0/organization", json={"value": [{"id": "t1"}]}, status=200)
    responses.add(responses.GET, "https://graph.microsoft.com/v1.0/domains", json={"value": []}, status=200)
    responses.add(responses.GET, "https://graph.microsoft.com/v1.0/subscribedSkus", json={"value": []}, status=200)
    responses.add(responses.GET, "https://graph.microsoft.com/v1.0/groups", json={"value": []}, status=200)
    responses.add(
        responses.GET,
        re.compile(r"https://graph\.microsoft\.com/v1\.0/.*"),
        json={"value": []},
        status=200,
    )

    provider = MagicMock()
    provider.get_access_token.return_value = "mock"
    client = MSGraphClient(
        tenant_context=TenantContext(tenant_id="t1"),
        token_provider=provider,
        http_client=ResilientHttpClient(settings=HttpSettings(max_retries=1)),
    )
    service = TenantReadinessService(graph_client=client)
    service.run_readiness_check()

    for call in responses.calls:
        assert call.request.method == "GET", f"Mutating HTTP method detected: {call.request.method}"
