import pytest
from unittest.mock import MagicMock, patch
from integration_service.onboarding import OnboardingService, OnboardingPlan
from integration_service.offboarding import OffboardingService
from integration_service.errors import InvalidPayloadError
from integration_service.clients.tenant_context import TenantContext
from integration_service.clients.ms_graph_client import MSGraphClient

@pytest.fixture
def mock_graph_client():
    client = MagicMock(spec=MSGraphClient)
    client.tenant_context = TenantContext("t1", "c1", "s1", "https://graph")
    return client

def test_onboarding_unapproved(mock_graph_client):
    svc = OnboardingService(graph_client=mock_graph_client)
    plan = OnboardingPlan(
        upn="lab-test@demo.com",
        display_name="Test",
        job_title="Dev",
        department="IT",
        usage_location="US",
        manager_upn="manager@demo.com",
        target_sku_id="sku123",
        target_sku_part_number="SKU_1",
        target_group_ids=["g1", "g2"],
        planned_mutations=[]
    )
    # execute without approval
    res = svc.execute_onboarding(plan, approved=False)
    assert res.stage == "unapproved"
    assert res.verification_passed is False
    # No graph posts should be made
    mock_graph_client.post.assert_not_called()

def test_onboarding_insufficient_license(mock_graph_client):
    svc = OnboardingService(graph_client=mock_graph_client)
    # Missing target_sku_id
    plan = OnboardingPlan(
        upn="lab-test@demo.com",
        display_name="Test",
        job_title="Dev",
        department="IT",
        usage_location="US",
        manager_upn=None,
        target_sku_id=None,
        target_sku_part_number=None,
        planned_mutations=[]
    )
    res = svc.execute_onboarding(plan, approved=True)
    assert res.stage == "failed"
    assert "Insufficient license capacity" in res.error_details

def test_onboarding_safety_boundary(mock_graph_client):
    svc = OnboardingService(graph_client=mock_graph_client)
    plan = OnboardingPlan(
        upn="real-user@demo.com",  # No LAB- prefix
        display_name="Real",
        job_title="Dev",
        department="IT",
        usage_location="US",
        manager_upn=None,
        target_sku_id="sku1",
        target_sku_part_number="SKU_1",
        planned_mutations=[]
    )
    with pytest.raises(InvalidPayloadError):
        svc.execute_onboarding(plan, approved=True)

def test_offboarding_removes_only_lab_groups(mock_graph_client):
    svc = OffboardingService(graph_client=mock_graph_client, protected_group_ids=["prot1"])
    
    # Mock capture state
    svc._capture_state = MagicMock(return_value={
        "id": "123",
        "accountEnabled": True,
        "groups": [
            {"id": "g1", "displayName": "LAB-TestGroup"},
            {"id": "g2", "displayName": "RealGroup"},
            {"id": "prot1", "displayName": "LAB-Protected"}
        ],
        "assignedLicenses": []
    })
    
    mock_graph_client.delete = MagicMock()
    
    res = svc.execute_offboarding("LAB-User@demo.com", approved=True)
    
    # Only g1 should be deleted (starts with LAB- and not protected)
    mock_graph_client.delete.assert_called_once_with("groups/g1/members/123/$ref", operation_name="OFFBOARD_REMOVE_GROUP")

def test_offboarding_idempotency(mock_graph_client):
    svc = OffboardingService(graph_client=mock_graph_client)
    
    svc._capture_state = MagicMock(return_value={
        "id": "123",
        "accountEnabled": False, # already disabled
        "groups": [], # no groups
        "assignedLicenses": [] # no licenses
    })
    
    mock_graph_client.patch = MagicMock()
    mock_graph_client.delete = MagicMock()
    
    res = svc.execute_offboarding("LAB-User@demo.com", approved=True)
    
    # Should not call patch to disable account because it's already disabled
    mock_graph_client.patch.assert_not_called()
    # Should not call delete for groups
    mock_graph_client.delete.assert_not_called()
    assert res.verification_passed is True

def test_offboarding_unapproved(mock_graph_client):
    svc = OffboardingService(graph_client=mock_graph_client)
    svc._capture_state = MagicMock(return_value={"id": "123"})
    
    res = svc.execute_offboarding("LAB-User@demo.com", approved=False)
    assert res.status == "unapproved"
    mock_graph_client.post.assert_not_called()

@patch('integration_service.remediation.TenantReadinessService.discover_lab_groups')
def test_duplicate_group_membership(mock_discover, mock_graph_client):
    mock_discover.return_value = [{"graph_id": "group123", "name": "LAB-Test"}]
    from integration_service.remediation import RemediationService
    svc = RemediationService(graph_client=mock_graph_client)
    # Mock user already in the group
    svc._capture_user_state = MagicMock(return_value={"id": "user123", "group_ids": ["group123"]})
    res = svc.execute_remediation("add_group", "LAB-User", target_group_id="group123", approved=True)
    # Should not call POST to add group because it's already there
    mock_graph_client.post.assert_not_called()
    assert res.status == "success"

def test_partial_onboarding_failure(mock_graph_client):
    svc = OnboardingService(graph_client=mock_graph_client)
    plan = OnboardingPlan(
        upn="LAB-test@demo.com", 
        display_name="Test", 
        job_title="Dev",
        department="IT",
        usage_location="US",
        manager_upn=None,
        target_sku_id="sku123", 
        target_sku_part_number="SKU_1",
        planned_mutations=[], 
        target_group_ids=["grp1"]
    )
    # Mock user creation success, but license assignment fails
    mock_graph_client.post.side_effect = [{"id": "user123"}, Exception("License API down")]
    # Read-back will fail because license is missing
    mock_graph_client.get.return_value = MagicMock(data={"id": "user123", "accountEnabled": True})
    res = svc.execute_onboarding(plan, approved=True)
    assert res.verification_passed is False
    assert res.stage == "failed"

def test_graph_succeeds_odoo_fails():
    from integration_service.operation_worker import OperationWorker
    # Mock Odoo client failing on write
    mock_odoo = MagicMock()
    mock_odoo.search_read.return_value = [{"id": 1, "x_state": "awaiting_approval"}]
    mock_odoo.write.side_effect = [
        True,  # first write to 'running'
        Exception("Odoo DB connection lost"), # write inside try block
        Exception("Odoo DB connection lost")  # write inside except block
    ]
    
    mock_ctx = MagicMock()
    mock_ctx.odoo = mock_odoo
    
    worker = OperationWorker(ctx=mock_ctx)
    worker.remediation_svc.execute_remediation = MagicMock(return_value=MagicMock(status="success", verification_passed=True))
    worker.remediation_svc.sync_to_odoo = MagicMock(return_value={"status": "fallback"})
    
    # Run the worker process
    worker.process_operation({
        "id": 1,
        "x_name": "REMEDIATE",
        "x_operation_type": "remediation",
        "x_target_upn": "lab-user@contoso.com",
        "x_remediation_action": "block_signin",
    })
    # Worker should attempt to write "uncertain" or handle the exception without crashing
    worker.remediation_svc.execute_remediation.assert_called_once()
    worker.remediation_svc.sync_to_odoo.assert_called_once()
    mock_odoo.write.assert_called()


def test_operation_worker_recovers_stale_running_after_restart():
    from integration_service.operation_worker import OperationWorker

    mock_odoo = MagicMock()
    mock_odoo.search_read.return_value = [
        {"id": 42, "x_name": "M365-ONBOARD-LAB-User", "x_started_at": "2026-08-15 00:00:00"}
    ]
    mock_odoo.write.return_value = True

    mock_ctx = MagicMock()
    mock_ctx.odoo = mock_odoo

    worker = OperationWorker(ctx=mock_ctx)
    worker.stale_running_seconds = 1
    recovered = worker.recover_stale_running_operations()

    assert recovered == 1
    search_args = mock_odoo.search_read.call_args.args
    assert search_args[0] == "x_m365_operation"
    assert search_args[1][0] == ["x_state", "=", "running"]
    assert search_args[1][1][0:2] == ["x_started_at", "<"]
    write_vals = mock_odoo.write.call_args.args[2]
    assert write_vals["x_state"] == "uncertain"
    assert "Verify Microsoft 365 state before retry" in write_vals["x_error_details"]
