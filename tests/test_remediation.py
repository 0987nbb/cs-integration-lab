# -*- coding: utf-8 -*-
"""Mocked unit tests for Helpdesk Remediation Actions Service."""
import re
from unittest.mock import MagicMock
import pytest
import responses

from integration_service.clients.ms_graph_client import MSGraphClient
from integration_service.clients.tenant_context import TenantContext
from integration_service.config import M365Settings
from integration_service.errors import InvalidPayloadError
from integration_service.http_client import HttpSettings, ResilientHttpClient
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


@responses.activate
def test_remediation_block_signin_execute_and_verify(m365_settings, mock_token_provider):
    # Pre-state -> accountEnabled=True
    responses.add(
        responses.GET,
        re.compile(r"https://graph\.microsoft\.com/v1\.0/users/lab-user@contoso\.com\?.*"),
        json={"id": "u-101", "userPrincipalName": "lab-user@contoso.com", "accountEnabled": True},
        status=200,
    )
    responses.add(responses.GET, "https://graph.microsoft.com/v1.0/users/lab-user@contoso.com/memberOf", json={"value": []}, status=200)

    # Patch -> accountEnabled=False
    responses.add(responses.PATCH, "https://graph.microsoft.com/v1.0/users/u-101", status=204)

    # Post-state -> accountEnabled=False
    responses.add(
        responses.GET,
        re.compile(r"https://graph\.microsoft\.com/v1\.0/users/u-101\?.*"),
        json={"id": "u-101", "userPrincipalName": "lab-user@contoso.com", "accountEnabled": False},
        status=200,
    )
    responses.add(responses.GET, "https://graph.microsoft.com/v1.0/users/u-101/memberOf", json={"value": []}, status=200)

    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=ResilientHttpClient(settings=HttpSettings(max_retries=1)),
    )
    service = RemediationService(graph_client=client)

    res = service.execute_remediation(action="block_signin", upn_or_id="lab-user@contoso.com", approved=True, dry_run=False)
    assert res.status == "success"
    assert res.verification_passed is True
    assert res.post_action_state["accountEnabled"] is False


def test_remediation_dry_run_captures_state_without_mutation():
    client = MagicMock(spec=MSGraphClient)
    client.tenant_context = TenantContext("t1", "c1", "secret", "https://graph.microsoft.com/v1.0")
    service = RemediationService(graph_client=client)
    service._capture_user_state = MagicMock(return_value={
        "id": "u-101",
        "upn": "lab-user@contoso.com",
        "accountEnabled": True,
        "assignedLicenses": [],
        "group_ids": [],
    })

    res = service.execute_remediation(
        action="block_signin",
        upn_or_id="lab-user@contoso.com",
        approved=False,
        dry_run=True,
    )

    assert res.status == "dry_run"
    assert res.verification_passed is True
    assert res.pre_action_state["id"] == "u-101"
    client.patch.assert_not_called()
    client.post.assert_not_called()
    client.delete.assert_not_called()


def test_remediation_rejects_unapproved_group_target():
    client = MagicMock(spec=MSGraphClient)
    client.tenant_context = TenantContext("t1", "c1", "secret", "https://graph.microsoft.com/v1.0")
    service = RemediationService(graph_client=client)

    with pytest.raises(InvalidPayloadError, match="approved LAB"):
        service.execute_remediation(
            action="add_group",
            upn_or_id="lab-user@contoso.com",
            target_group_id="real-group",
            approved=True,
            dry_run=False,
        )

    client.post.assert_not_called()


def test_remediation_assign_license_rejects_zero_capacity():
    client = MagicMock(spec=MSGraphClient)
    client.tenant_context = TenantContext("t1", "c1", "secret", "https://graph.microsoft.com/v1.0")
    service = RemediationService(graph_client=client)
    service._capture_user_state = MagicMock(return_value={
        "id": "u-101",
        "assignedLicenses": [],
        "group_ids": [],
    })

    with pytest.raises(InvalidPayloadError, match="no available license capacity"):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "integration_service.remediation.TenantReadinessService.discover_skus",
                lambda _self: {"skus": [{"sku_id": "sku-zero", "sku_part_number": "E5", "available_units": 0}]},
            )
            service.execute_remediation(
                action="assign_license",
                upn_or_id="lab-user@contoso.com",
                target_sku_id="sku-zero",
                approved=True,
                dry_run=False,
            )

    client.post.assert_not_called()


def test_remediation_add_group_idempotent_when_already_member():
    client = MagicMock(spec=MSGraphClient)
    client.tenant_context = TenantContext("t1", "c1", "secret", "https://graph.microsoft.com/v1.0")
    service = RemediationService(graph_client=client)
    service._capture_user_state = MagicMock(side_effect=[
        {"id": "u-101", "group_ids": ["g-lab"], "assignedLicenses": []},
        {"id": "u-101", "group_ids": ["g-lab"], "assignedLicenses": []},
    ])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "integration_service.remediation.TenantReadinessService.discover_lab_groups",
            lambda _self, prefix="LAB-": [{"graph_id": "g-lab", "name": "LAB-Helpdesk"}],
        )
        res = service.execute_remediation(
            action="add_group",
            upn_or_id="lab-user@contoso.com",
            target_group_id="g-lab",
            approved=True,
            dry_run=False,
        )

    assert res.status == "success"
    assert res.verification_passed is True
    assert "no mutation needed" in res.details
    client.post.assert_not_called()


def test_remediation_assign_license_idempotent_when_already_assigned_even_if_capacity_zero():
    client = MagicMock(spec=MSGraphClient)
    client.tenant_context = TenantContext("t1", "c1", "secret", "https://graph.microsoft.com/v1.0")
    service = RemediationService(graph_client=client)
    service._capture_user_state = MagicMock(side_effect=[
        {"id": "u-101", "group_ids": [], "assignedLicenses": [{"skuId": "sku-zero"}]},
        {"id": "u-101", "group_ids": [], "assignedLicenses": [{"skuId": "sku-zero"}]},
    ])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "integration_service.remediation.TenantReadinessService.discover_skus",
            lambda _self: {"skus": [{"sku_id": "sku-zero", "sku_part_number": "E5", "available_units": 0}]},
        )
        res = service.execute_remediation(
            action="assign_license",
            upn_or_id="lab-user@contoso.com",
            target_sku_id="sku-zero",
            approved=True,
            dry_run=False,
        )

    assert res.status == "success"
    assert res.verification_passed is True
    assert "no mutation needed" in res.details
    client.post.assert_not_called()


@responses.activate
def test_remediation_revoke_sessions_uses_post_action_readback(m365_settings, mock_token_provider):
    responses.add(
        responses.GET,
        re.compile(r"https://graph\.microsoft\.com/v1\.0/users/lab-user@contoso\.com\?.*"),
        json={
            "id": "u-101",
            "userPrincipalName": "lab-user@contoso.com",
            "accountEnabled": True,
            "signInSessionsValidFromDateTime": "2026-08-15T01:00:00Z",
        },
        status=200,
    )
    responses.add(responses.GET, "https://graph.microsoft.com/v1.0/users/lab-user@contoso.com/memberOf", json={"value": []}, status=200)
    responses.add(responses.POST, "https://graph.microsoft.com/v1.0/users/u-101/revokeSignInSessions", json={"value": True}, status=200)
    responses.add(
        responses.GET,
        re.compile(r"https://graph\.microsoft\.com/v1\.0/users/u-101\?.*"),
        json={
            "id": "u-101",
            "userPrincipalName": "lab-user@contoso.com",
            "accountEnabled": True,
            "signInSessionsValidFromDateTime": "2026-08-15T01:05:00Z",
        },
        status=200,
    )
    responses.add(responses.GET, "https://graph.microsoft.com/v1.0/users/u-101/memberOf", json={"value": []}, status=200)

    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=ResilientHttpClient(settings=HttpSettings(max_retries=1)),
    )
    service = RemediationService(graph_client=client)

    res = service.execute_remediation(action="revoke_sessions", upn_or_id="lab-user@contoso.com", approved=True, dry_run=False)
    assert res.status == "success"
    assert res.verification_passed is True
    assert "signInSessionsValidFromDateTime" in res.verification_method


@responses.activate
def test_remediation_reset_password_verifies_by_post_action_user_readback(m365_settings, mock_token_provider):
    responses.add(
        responses.GET,
        re.compile(r"https://graph\.microsoft\.com/v1\.0/users/lab-user@contoso\.com\?.*"),
        json={"id": "u-101", "userPrincipalName": "lab-user@contoso.com", "accountEnabled": True},
        status=200,
    )
    responses.add(responses.GET, "https://graph.microsoft.com/v1.0/users/lab-user@contoso.com/memberOf", json={"value": []}, status=200)
    responses.add(responses.PATCH, "https://graph.microsoft.com/v1.0/users/u-101", status=204)
    responses.add(
        responses.GET,
        re.compile(r"https://graph\.microsoft\.com/v1\.0/users/u-101\?.*"),
        json={"id": "u-101", "userPrincipalName": "lab-user@contoso.com", "accountEnabled": True},
        status=200,
    )
    responses.add(responses.GET, "https://graph.microsoft.com/v1.0/users/u-101/memberOf", json={"value": []}, status=200)

    client = MSGraphClient(
        tenant_context=TenantContext.from_settings(m365_settings),
        token_provider=mock_token_provider,
        http_client=ResilientHttpClient(settings=HttpSettings(max_retries=1)),
    )
    service = RemediationService(graph_client=client)

    res = service.execute_remediation(
        action="reset_password",
        upn_or_id="lab-user@contoso.com",
        new_temp_password="LAB#Temp2026!",
        approved=True,
        dry_run=False,
    )
    assert res.status == "success"
    assert res.verification_passed is True
    assert "passwordProfile is write-only" in res.verification_method
