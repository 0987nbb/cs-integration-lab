# -*- coding: utf-8 -*-
"""
Automated offline test suite for Phase 3B UI Testing Playground / Automation Resilience Workflow.
Mocks Playwright page objects and Odoo API calls to run 100% offline without network dependency.
"""
import json
import pytest
from unittest.mock import MagicMock, patch

from integration_service.rpa_worker.config import WorkerConfig
from integration_service.rpa_worker.exceptions import (
    PermanentWorkerError,
    TransientWorkerError,
    HumanInterventionRequiredError,
)
from integration_service.rpa_worker.models import RpaJobRecord, JobPayload, ExecutionResult
from integration_service.rpa_worker.job_processor import JobProcessor
from integration_service.rpa_worker.workflows.ui_playground_workflow import run_ui_playground_workflow


class TestUiPlaygroundWorkflow:
    """Offline unit test suite for UI Testing Playground workflow."""

    def test_01_successful_dynamic_id_workflow(self):
        """1. Successful dynamic_id scenario using resilient class selector."""
        mock_page = MagicMock()
        mock_page.is_visible.return_value = True
        mock_page.get_text = MagicMock(return_value="Button with Dynamic ID")
        mock_page.text_content.return_value = "Button with Dynamic ID"

        payload = JobPayload(
            raw_payload="",
            data={"scenario": "dynamic_id", "expected_text": "Button with Dynamic ID"},
            job_type="ui_playground",
        )

        res = run_ui_playground_workflow(mock_page, payload)
        assert res["status"] == "completed"
        assert res["scenario"] == "dynamic_id"
        assert res["verified"] is True
        assert res["actual_text"] == "Button with Dynamic ID"
        assert res["resilient_selector_used"] == "button.btn-primary"

    def test_02_successful_load_delay_workflow(self):
        """2. Successful load_delay scenario using explicit wait for server delay."""
        mock_page = MagicMock()
        mock_page.is_visible.return_value = True
        mock_page.text_content.return_value = "Button Appearing After Delay"

        payload = JobPayload(
            raw_payload="",
            data={"scenario": "load_delay", "expected_button_text": "Button Appearing After Delay"},
            job_type="ui_playground",
        )

        res = run_ui_playground_workflow(mock_page, payload)
        assert res["status"] == "completed"
        assert res["scenario"] == "load_delay"
        assert res["verified"] is True
        assert res["explicit_wait_used"] is True

    def test_03_successful_ajax_data_workflow(self):
        """3. Successful ajax_data scenario using explicit wait for asynchronous AJAX container."""
        mock_page = MagicMock()
        mock_page.is_visible.return_value = True
        mock_page.text_content.return_value = "Data loaded with AJAX get request."

        payload = JobPayload(
            raw_payload="",
            data={"scenario": "ajax_data", "expected_text": "Data loaded with AJAX get request."},
            job_type="ui_playground",
        )

        res = run_ui_playground_workflow(mock_page, payload)
        assert res["status"] == "completed"
        assert res["scenario"] == "ajax_data"
        assert res["verified"] is True
        assert "AJAX" in res["actual_text"]

    def test_04_unsupported_scenario_rejected(self):
        """4. Unsupported scenario is rejected in pre-execution validation."""
        job = RpaJobRecord(
            id=301,
            name="RPA/2026/00301",
            job_type="ui_playground",
            payload_str=json.dumps({"scenario": "invalid_scenario_name"}),
            state="running",
            idempotency_key="TEST-PLAY-301",
        )
        processor = JobProcessor()
        res = processor.process_job(job)
        assert res.state == "failed"
        assert "Unsupported scenario" in res.error_details

    def test_05_expected_text_mismatch_raises_permanent_failure(self):
        """5. Result text mismatch raises PermanentWorkerError with details."""
        mock_page = MagicMock()
        mock_page.is_visible.return_value = True
        mock_page.text_content.return_value = "Unexpected Wrong Text"

        payload = JobPayload(
            raw_payload="",
            data={"scenario": "dynamic_id", "expected_text": "Button with Dynamic ID"},
            job_type="ui_playground",
        )

        with pytest.raises(PermanentWorkerError, match="Dynamic ID verification failed"):
            run_ui_playground_workflow(mock_page, payload)

    def test_06_job_processor_dispatches_ui_playground(self):
        """6. JobProcessor correctly dispatches job_type 'ui_playground'."""
        job = RpaJobRecord(
            id=302,
            name="RPA/2026/00302",
            job_type="ui_playground",
            payload_str=json.dumps({"scenario": "dynamic_id", "expected_text": "Button with Dynamic ID"}),
            state="running",
            idempotency_key="TEST-PLAY-302",
        )

        def custom_task(page, payload):
            return {"status": "completed", "workflow": "ui_playground", "scenario": payload.data["scenario"]}

        processor = JobProcessor()
        res = processor.process_job(job, custom_task_func=custom_task)
        assert res.state == "success"
        assert res.result_data["workflow"] == "ui_playground"

    def test_07_regression_saucedemo_dispatch_still_works(self):
        """7. Regression check: JobProcessor continues to dispatch job_type 'saucedemo' correctly."""
        job = RpaJobRecord(
            id=303,
            name="RPA/2026/00303",
            job_type="saucedemo",
            payload_str=json.dumps({
                "product_name": "Sauce Labs Backpack",
                "checkout": {"first_name": "Ali", "last_name": "Raza", "postal_code": "46000"}
            }),
            state="running",
            idempotency_key="TEST-PLAY-303",
        )

        def custom_task(page, payload):
            return {"status": "completed", "workflow": "saucedemo_checkout"}

        processor = JobProcessor()
        res = processor.process_job(job, custom_task_func=custom_task)
        assert res.state == "success"
        assert res.result_data["workflow"] == "saucedemo_checkout"
