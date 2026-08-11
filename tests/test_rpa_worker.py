# -*- coding: utf-8 -*-
"""
Automated offline test suite for Phase 2 RPA Worker foundation.
Uses mocks/fakes for Odoo API and Playwright browser layer.
"""
from datetime import datetime, timezone
import json
import pytest
from unittest.mock import MagicMock, patch

from integration_service.rpa_worker.config import WorkerConfig
from integration_service.rpa_worker.exceptions import (
    WorkerError,
    TransientWorkerError,
    PermanentWorkerError,
    HumanInterventionRequiredError,
)
from integration_service.rpa_worker.logging_utils import (
    sanitize_sensitive_data,
    RedactingJsonFormatter,
)
from integration_service.rpa_worker.models import RpaJobRecord, JobPayload, ExecutionResult
from integration_service.rpa_worker.odoo_claim import OdooJobClaimer
from integration_service.rpa_worker.job_processor import JobProcessor
from integration_service.rpa_worker.worker import RpaWorker
from integration_service.rpa_worker.browser.base import PlaywrightBrowserManager


class MockOdooClient:
    """Mock Odoo Client simulating server responses and data storage for offline testing."""

    def __init__(self, records=None):
        self.records = records or []

    def search_read(self, model, domain, limit=None, **kwargs):
        out = []
        for r in self.records:
            match = True
            for criterion in domain:
                if len(criterion) == 3:
                    f, op, val = criterion
                    rec_val = r.get(f)
                    if op == "=" and rec_val != val:
                        match = False
                    elif op == "!=" and rec_val == val:
                        match = False
            if match:
                out.append(r)
        if limit:
            out = out[:limit]
        return out

    def search(self, model, domain, **kwargs):
        return [r["id"] for r in self.search_read(model, domain, **kwargs)]

    def write(self, model, ids, vals):
        for r in self.records:
            if r["id"] in ids:
                r.update(vals)
        return True


class TestRpaWorkerFoundation:
    """Offline unit test suite for Phase 2 RPA Worker Foundation."""

    def test_01_empty_queue_handling(self):
        """1. Empty queue handling returns False cleanly."""
        client = MockOdooClient(records=[])
        claimer = OdooJobClaimer(client=client)
        worker = RpaWorker(claimer=claimer)
        
        processed = worker.process_next_job()
        assert processed is False

    def test_02_queued_job_successfully_claimed(self):
        """2. Queued job is successfully claimed: state -> running, started_at set."""
        record = {
            "id": 101,
            "x_name": "RPA/2026/00101",
            "x_job_type": "saucedemo",
            "x_payload": '{"product_name": "Sauce Labs Backpack"}',
            "x_state": "queued",
            "x_idempotency_key": "IDEMP-101",
            "x_attempt_count": 0,
        }
        client = MockOdooClient(records=[record])
        claimer = OdooJobClaimer(client=client)
        
        job = RpaJobRecord.from_odoo_dict(record)
        claimed = claimer.claim_job(job)
        
        assert claimed is not None
        assert claimed.state == "running"
        assert claimed.started_at is not None
        assert record["x_state"] == "running"

    def test_03_already_running_or_terminal_job_not_claimed(self):
        """3. Already-running or terminal job claim fails and is skipped."""
        record = {
            "id": 102,
            "x_name": "RPA/2026/00102",
            "x_job_type": "saucedemo",
            "x_payload": '{"product_name": "Bike Light"}',
            "x_state": "running",
            "x_idempotency_key": "IDEMP-102",
        }
        client = MockOdooClient(records=[record])
        claimer = OdooJobClaimer(client=client)
        
        job = RpaJobRecord.from_odoo_dict(record)
        claimed = claimer.claim_job(job)
        assert claimed is None

    def test_04_invalid_payload_rejected(self):
        """4. Invalid payload JSON is rejected as permanent failure (not browser error)."""
        job = RpaJobRecord(
            id=103,
            name="RPA/2026/00103",
            job_type="saucedemo",
            payload_str="{bad_json_format:",
            state="running",
            idempotency_key="IDEMP-103",
        )
        processor = JobProcessor()
        res = processor.process_job(job)
        
        assert res.state == "failed"
        assert "Validation Failure" in res.error_details

    def test_05_unsupported_job_type_rejected(self):
        """5. Unsupported job_type is rejected as permanent failure."""
        job = RpaJobRecord(
            id=104,
            name="RPA/2026/00104",
            job_type="unknown_workflow",
            payload_str='{"data": 1}',
            state="running",
            idempotency_key="IDEMP-104",
        )
        processor = JobProcessor()
        res = processor.process_job(job)
        
        assert res.state == "failed"
        assert "Unsupported job_type" in res.error_details

    def test_06_browser_timeout_classified_correctly(self):
        """6. Browser timeout is classified as TransientWorkerError -> failed state."""
        job = RpaJobRecord(
            id=105,
            name="RPA/2026/00105",
            job_type="saucedemo",
            payload_str='{"product_name": "Backpack"}',
            state="running",
            idempotency_key="IDEMP-105",
        )
        
        def failing_task(page, payload):
            raise TransientWorkerError("Navigation timeout waiting for page load.")

        processor = JobProcessor()
        res = processor.process_job(job, custom_task_func=failing_task)
        
        assert res.state == "failed"
        assert "Transient Failure" in res.error_details

    def test_07_authentication_failure_classified_correctly(self):
        """7. Permanent execution error classified correctly -> failed state."""
        job = RpaJobRecord(
            id=106,
            name="RPA/2026/00106",
            job_type="saucedemo",
            payload_str='{"product_name": "Backpack"}',
            state="running",
            idempotency_key="IDEMP-106",
        )
        
        def auth_failing_task(page, payload):
            raise PermanentWorkerError("Invalid username or password.")

        processor = JobProcessor()
        res = processor.process_job(job, custom_task_func=auth_failing_task)
        
        assert res.state == "failed"
        assert "Permanent Failure" in res.error_details

    def test_08_captcha_2fa_classified_as_needs_human(self):
        """8. CAPTCHA / 2FA challenge classified as HumanInterventionRequiredError -> needs_human state."""
        job = RpaJobRecord(
            id=107,
            name="RPA/2026/00107",
            job_type="saucedemo",
            payload_str='{"product_name": "Backpack"}',
            state="running",
            idempotency_key="IDEMP-107",
        )
        
        def captcha_task(page, payload):
            raise HumanInterventionRequiredError("hCaptcha challenge encountered on login page.")

        processor = JobProcessor()
        res = processor.process_job(job, custom_task_func=captcha_task)
        
        assert res.state == "needs_human"
        assert "Human Intervention Required" in res.error_details

    def test_09_browser_failure_produces_screenshot_attempt(self):
        """9. Playwright browser manager attempts screenshot capture on failure."""
        config = WorkerConfig(headless=True)
        mgr = PlaywrightBrowserManager(config=config)
        
        # Mock task throwing exception and check screenshot logic
        mock_page = MagicMock()
        mock_page.screenshot.return_value = b"fake_png_bytes"
        
        b64 = mgr._capture_screenshot_b64(mock_page)
        assert b64 is not None
        assert isinstance(b64, str)

    def test_10_odoo_result_write_failure_handled(self):
        """10. Odoo result write failure is handled gracefully."""
        client = MockOdooClient(records=[])
        client.write = MagicMock(side_effect=Exception("API write error"))
        
        claimer = OdooJobClaimer(client=client)
        res = ExecutionResult(state="success", result_data={"status": "ok"})
        
        success = claimer.write_result(108, res)
        assert success is False

    def test_11_duplicate_idempotent_processing_prevented(self):
        """11. Compare-and-update claim mechanism prevents duplicate job execution."""
        record = {
            "id": 109,
            "x_name": "RPA/2026/00109",
            "x_job_type": "saucedemo",
            "x_payload": '{"product_name": "Backpack"}',
            "x_state": "queued",
            "x_idempotency_key": "IDEMP-109",
        }
        client = MockOdooClient(records=[record])
        claimer1 = OdooJobClaimer(client=client)
        claimer2 = OdooJobClaimer(client=client)
        
        job = RpaJobRecord.from_odoo_dict(record)
        
        # Worker 1 claims job
        claimed1 = claimer1.claim_job(job)
        assert claimed1 is not None
        
        # Worker 2 attempts to claim same job
        claimed2 = claimer2.claim_job(job)
        assert claimed2 is None

    def test_12_worker_cleanup_after_exception(self):
        """12. Browser manager cleans up page, context, browser in finally block."""
        import sys
        mgr = PlaywrightBrowserManager()
        
        mock_playwright_obj = MagicMock()
        mock_browser = MagicMock()
        mock_context = MagicMock()
        mock_page = MagicMock()
        
        mock_playwright_obj.chromium.launch.return_value = mock_browser
        mock_browser.new_context.return_value = mock_context
        mock_context.new_page.return_value = mock_page
        
        mock_sync_playwright = MagicMock()
        mock_sync_playwright.return_value.start.return_value = mock_playwright_obj

        class MockPlaywrightTimeoutError(Exception):
            pass

        class MockPlaywrightError(Exception):
            pass

        mock_module = MagicMock()
        mock_module.sync_playwright = mock_sync_playwright
        mock_module.TimeoutError = MockPlaywrightTimeoutError
        mock_module.Error = MockPlaywrightError

        def failing_task(page):
            raise ValueError("Task error inside browser")

        with patch.dict(sys.modules, {"playwright": mock_module, "playwright.sync_api": mock_module}):
            with pytest.raises(PermanentWorkerError):
                mgr.run_browser_task(failing_task)
            
            mock_page.close.assert_called_once()
            mock_context.close.assert_called_once()
            mock_browser.close.assert_called_once()
            mock_playwright_obj.stop.assert_called_once()

    def test_13_stale_running_recovery_behavior(self):
        """13. Jobs left in running state > timeout threshold are recovered to failed state."""
        stale_record = {
            "id": 110,
            "x_name": "RPA/2026/00110",
            "x_state": "running",
            "x_started_at": "2026-01-01 10:00:00",  # Old timestamp
        }
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        active_record = {
            "id": 111,
            "x_name": "RPA/2026/00111",
            "x_state": "running",
            "x_started_at": now_utc,
        }
        client = MockOdooClient(records=[stale_record, active_record])
        config = WorkerConfig(stale_running_timeout_seconds=300)
        claimer = OdooJobClaimer(client=client, config=config)
        
        recovered_count = claimer.recover_stale_running_jobs()
        assert recovered_count == 1
        assert stale_record["x_state"] == "failed"
        assert "Stale running recovery" in stale_record["x_error_details"]
        assert active_record["x_state"] == "running"

    def test_14_credentials_not_written_to_logs(self):
        """14. Credentials, passwords, and API keys are redacted from logs and data structures."""
        sensitive_dict = {
            "api_key": "secret_key_12345",
            "password": "super_secret_password",
            "normal_field": "public_data",
            "nested": {
                "token": "bearer_abc123xyz",
            }
        }
        sanitized = sanitize_sensitive_data(sensitive_dict)
        
        assert sanitized["api_key"] == "[REDACTED]"
        assert sanitized["password"] == "[REDACTED]"
        assert sanitized["normal_field"] == "public_data"
        assert sanitized["nested"]["token"] == "[REDACTED]"

        config = WorkerConfig(odoo_api_key="secret_api_key_8899")
        assert "secret_api_key_8899" not in repr(config)

    def test_15_competing_concurrent_workers_claim_only_once(self):
        """15. Simulates 5 competing worker threads attempting to claim the exact same queued job simultaneously."""
        import threading
        record = {
            "id": 501,
            "x_name": "RPA/2026/00501",
            "x_job_type": "saucedemo",
            "x_payload": '{"product_name": "Backpack"}',
            "x_state": "queued",
            "x_idempotency_key": "CONCURRENT-CLAIM-501",
            "x_attempt_count": 0,
        }
        client = MockOdooClient(records=[record])
        claimer = OdooJobClaimer(client=client)
        job = RpaJobRecord.from_odoo_dict(record)

        barrier = threading.Barrier(5)
        claimed_results = []
        failed_results = []
        lock = threading.Lock()

        def worker_claim_task():
            barrier.wait()  # Synchronize all 5 threads to launch claim at the exact same instant
            res = claimer.claim_job(job)
            with lock:
                if res is not None:
                    claimed_results.append(res)
                else:
                    failed_results.append(res)

        threads = [threading.Thread(target=worker_claim_task) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly 1 worker thread claims the job, remaining 4 get None (claim failure)
        assert len(claimed_results) == 1
        assert len(failed_results) == 4
        assert claimed_results[0].state == "running"
        assert record["x_state"] == "running"

    def test_16_external_success_odoo_write_fail_reconciliation_no_duplicate_execution(self, tmp_path):
        """
        16. Tests resiliency pattern:
        External action succeeds -> Odoo result write fails -> worker/process restarts ->
        job is recovered -> prior external outcome is verified -> action is NOT executed twice.
        """
        from integration_service.rpa_worker.job_processor import OutcomeReconciler

        ledger_file = str(tmp_path / "rpa_outcome_ledger.json")
        reconciler1 = OutcomeReconciler(ledger_file=ledger_file)
        processor1 = JobProcessor(reconciler=reconciler1)

        job = RpaJobRecord(
            id=601,
            name="RPA/2026/00601",
            job_type="saucedemo",
            payload_str=json.dumps({
                "product_name": "Sauce Labs Backpack",
                "checkout": {"first_name": "Ali", "last_name": "Raza", "postal_code": "46000"}
            }),
            state="running",
            idempotency_key="RECONCILE-TEST-601",
        )

        action_mock = MagicMock(return_value={"status": "completed", "order_id": "ORDER-9999"})
        readback_mock = MagicMock(return_value={"result_data": {"status": "completed", "order_id": "ORDER-9999"}, "external_reference": "ORDER-9999"})

        # Step 1: External action succeeds, but Odoo write fails
        res1 = processor1.process_job(job, custom_task_func=action_mock)
        assert res1.state == "success"
        assert action_mock.call_count == 1

        # Simulate Odoo write failure
        client = MockOdooClient(records=[])
        client.write = MagicMock(side_effect=Exception("Odoo DB connection lost"))
        claimer = OdooJobClaimer(client=client)
        write_success = claimer.write_result(job.id, res1)
        assert write_success is False

        # Step 2: Worker process restarts (fresh processor instance loading same outcome ledger & external readback)
        reconciler2 = OutcomeReconciler(ledger_file=ledger_file)
        processor2 = JobProcessor(reconciler=reconciler2)
        action_mock.reset_mock()

        # Retried / recovered job is processed again with external read-back verification
        res2 = processor2.process_job(job, custom_task_func=action_mock, external_readback_func=readback_mock)

        # Verification assertions:
        assert readback_mock.call_count >= 1     # External read-back query was executed
        assert res2.state == "success"          # Final job state is successful/reconciled
        assert res2.last_successful_step == "reconciled_prior_outcome"
        assert action_mock.call_count == 0       # State-changing external action was NOT executed a second time!
