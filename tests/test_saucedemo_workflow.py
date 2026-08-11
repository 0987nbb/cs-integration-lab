# -*- coding: utf-8 -*-
"""
Automated offline test suite for Phase 3A SauceDemo Browser Automation Workflow.
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
from integration_service.rpa_worker.odoo_claim import OdooJobClaimer
from integration_service.rpa_worker.workflows.saucedemo_workflow import run_saucedemo_workflow


class TestSauceDemoWorkflow:
    """Offline unit test suite for SauceDemo workflow."""

    def test_01_valid_payload_parsing(self):
        """1. Valid SauceDemo payload passes pre-execution validation."""
        job = RpaJobRecord(
            id=201,
            name="RPA/2026/00201",
            job_type="saucedemo",
            payload_str=json.dumps({
                "product_name": "Sauce Labs Backpack",
                "checkout": {"first_name": "Ali", "last_name": "Raza", "postal_code": "46000"}
            }),
            state="running",
            idempotency_key="TEST-SAUCE-201",
        )
        processor = JobProcessor()
        payload = processor.validate_job_payload(job)
        assert payload.data["product_name"] == "Sauce Labs Backpack"
        assert payload.data["checkout"]["first_name"] == "Ali"

    def test_02_missing_product_name_rejected(self):
        """2. Missing product_name is rejected during pre-execution validation."""
        job = RpaJobRecord(
            id=202,
            name="RPA/2026/00202",
            job_type="saucedemo",
            payload_str=json.dumps({"checkout": {"first_name": "Ali", "last_name": "Raza", "postal_code": "46000"}}),
            state="running",
            idempotency_key="TEST-SAUCE-202",
        )
        processor = JobProcessor()
        res = processor.process_job(job)
        assert res.state == "failed"
        assert "Missing required 'product_name'" in res.error_details

    def test_03_missing_checkout_info_rejected(self):
        """3. Missing required checkout fields (postal_code) rejected during pre-execution validation."""
        job = RpaJobRecord(
            id=203,
            name="RPA/2026/00203",
            job_type="saucedemo",
            payload_str=json.dumps({
                "product_name": "Sauce Labs Bike Light",
                "checkout": {"first_name": "Ali", "last_name": "Raza", "postal_code": ""}
            }),
            state="running",
            idempotency_key="TEST-SAUCE-203",
        )
        processor = JobProcessor()
        res = processor.process_job(job)
        assert res.state == "failed"
        assert "checkout" in res.error_details.lower()

    def test_04_invalid_json_payload_rejected(self):
        """4. Malformed JSON payload is rejected without launching browser."""
        job = RpaJobRecord(
            id=204,
            name="RPA/2026/00204",
            job_type="saucedemo",
            payload_str="{bad_json:",
            state="running",
            idempotency_key="TEST-SAUCE-204",
        )
        processor = JobProcessor()
        res = processor.process_job(job)
        assert res.state == "failed"
        assert "Validation Failure" in res.error_details

    def test_05_product_not_found_raises_permanent_failure(self):
        """5. Product not found in inventory raises permanent failure."""
        mock_page = MagicMock()
        mock_page.content.return_value = "<html><body>Inventory</body></html>"
        mock_page.query_selector_all.return_value = []  # No products found
        mock_page.is_visible.return_value = True

        payload = JobPayload(
            raw_payload="",
            data={"product_name": "Nonexistent Product", "checkout": {"first_name": "Ali", "last_name": "Raza", "postal_code": "46000"}},
            job_type="saucedemo",
        )

        with pytest.raises(PermanentWorkerError, match="not found in SauceDemo inventory"):
            run_saucedemo_workflow(mock_page, payload)

    def test_06_successful_saucedemo_workflow_execution(self):
        """6. End-to-end successful SauceDemo workflow using Page Object mocks."""
        mock_page = MagicMock()
        mock_page.content.return_value = "<html><body>Inventory</body></html>"
        
        # Mock locator & selector query results
        mock_item = MagicMock()
        mock_name_el = MagicMock()
        mock_name_el.text_content.return_value = "Sauce Labs Backpack"
        mock_price_el = MagicMock()
        mock_price_el.text_content.return_value = "$29.99"
        mock_btn = MagicMock()
        
        def item_query(sel):
            if sel == ".inventory_item_name":
                return mock_name_el
            if sel == ".inventory_item_price":
                return mock_price_el
            if sel == "button":
                return mock_btn
            return None
        
        mock_item.query_selector.side_effect = item_query
        mock_page.query_selector_all.return_value = [mock_item]

        def is_visible_mock(selector, **kwargs):
            if selector in (".inventory_list", ".cart_list", "#checkout_complete_container", ".complete-header"):
                return True
            if selector == ".shopping_cart_badge":
                return True
            return True

        mock_page.is_visible.side_effect = is_visible_mock
        mock_page.text_content.side_effect = lambda sel: {
            ".shopping_cart_badge": "1",
            ".complete-header": "Thank you for your order!",
            ".title": "Products",
        }.get(sel, "")

        payload = JobPayload(
            raw_payload="",
            data={
                "product_name": "Sauce Labs Backpack",
                "checkout": {"first_name": "Ali", "last_name": "Raza", "postal_code": "46000"}
            },
            job_type="saucedemo",
        )

        res = run_saucedemo_workflow(mock_page, payload)
        assert res["status"] == "completed"
        assert res["product_name"] == "Sauce Labs Backpack"
        assert res["cart_verified"] is True
        assert res["checkout_verified"] is True
        assert res["order_confirmation_verified"] is True
        assert res["confirmation_header"] == "Thank you for your order!"

    def test_07_login_failure_raises_permanent_error(self):
        """7. Login failure raises PermanentWorkerError with error message."""
        mock_page = MagicMock()
        mock_page.content.return_value = "<html><body>Login</body></html>"

        def is_visible_mock(selector, **kwargs):
            if selector in ('[data-test="error"]', '.error-message-container'):
                return True
            return False

        mock_page.is_visible.side_effect = is_visible_mock
        mock_page.text_content.return_value = "Epic sadface: Username and password do not match any user in this service"

        payload = JobPayload(
            raw_payload="",
            data={"product_name": "Sauce Labs Backpack", "checkout": {"first_name": "Ali", "last_name": "Raza", "postal_code": "46000"}},
            job_type="saucedemo",
        )

        with pytest.raises(PermanentWorkerError, match="authentication failed"):
            run_saucedemo_workflow(mock_page, payload)

    def test_08_captcha_challenge_raises_needs_human(self):
        """8. CAPTCHA / 2FA challenge on login page raises HumanInterventionRequiredError."""
        mock_page = MagicMock()
        mock_page.content.return_value = "<html><body>g-recaptcha captcha challenge</body></html>"
        mock_page.is_visible.return_value = False

        payload = JobPayload(
            raw_payload="",
            data={"product_name": "Sauce Labs Backpack", "checkout": {"first_name": "Ali", "last_name": "Raza", "postal_code": "46000"}},
            job_type="saucedemo",
        )

        with pytest.raises(HumanInterventionRequiredError, match="CAPTCHA"):
            run_saucedemo_workflow(mock_page, payload)

    def test_09_final_success_page_verification(self):
        """9. Verification confirms that status 'completed' is set only when order confirmation page is detected."""
        mock_page = MagicMock()
        mock_page.content.return_value = "<html><body>Checkout Complete</body></html>"
        mock_page.is_visible.side_effect = lambda sel, **kw: sel in ('.inventory_list', '.cart_list', '.complete-header', '.shopping_cart_badge', '[data-test="finish"]')
        mock_page.text_content.side_effect = lambda sel: "1" if sel == '.shopping_cart_badge' else ("Thank you for your order!" if sel == '.complete-header' else "")
        
        mock_item = MagicMock()
        mock_item.query_selector.side_effect = lambda s: MagicMock(text_content=MagicMock(return_value="Sauce Labs Backpack" if s == ".inventory_item_name" else "$29.99"))
        mock_page.query_selector_all.return_value = [mock_item]

        payload = JobPayload(
            raw_payload="",
            data={"product_name": "Sauce Labs Backpack", "checkout": {"first_name": "Ali", "last_name": "Raza", "postal_code": "46000"}},
            job_type="saucedemo",
        )

        res = run_saucedemo_workflow(mock_page, payload)
        assert res["order_confirmation_verified"] is True
        assert "Thank you" in res["confirmation_header"]

    def test_10_odoo_result_write_failure_after_success(self):
        """10. Odoo write failure after browser workflow completion is handled safely."""
        from integration_service.rpa_worker.models import ExecutionResult
        from integration_service.rpa_worker.odoo_claim import OdooJobClaimer

        client = MagicMock()
        client.write.side_effect = Exception("Odoo write HTTP 500 error")

        claimer = OdooJobClaimer(client=client)
        exec_res = ExecutionResult(state="success", result_data={"status": "completed"})

        success = claimer.write_result(301, exec_res)
        assert success is False

    def test_11_saucedemo_credentials_loaded_from_env(self):
        """11. SauceDemo credentials are loaded from environment variables."""
        import os
        with patch.dict(os.environ, {"SAUCEDEMO_USERNAME": "test_user_env", "SAUCEDEMO_PASSWORD": "test_pwd_env"}):
            cfg = WorkerConfig()
            assert cfg.saucedemo_username == "test_user_env"
            assert cfg.saucedemo_password == "test_pwd_env"

    def test_12_missing_saucedemo_credentials_raises_error(self):
        """12. Missing SauceDemo credentials in config raises PermanentWorkerError during workflow execution."""
        mock_page = MagicMock()
        cfg = WorkerConfig(saucedemo_username="", saucedemo_password="")
        payload = JobPayload(
            raw_payload="",
            data={"product_name": "Sauce Labs Backpack", "checkout": {"first_name": "Ali", "last_name": "Raza", "postal_code": "46000"}},
            job_type="saucedemo",
        )
        with pytest.raises(PermanentWorkerError, match="Missing required SauceDemo credentials"):
            run_saucedemo_workflow(mock_page, payload, config=cfg)

    def test_13_worker_config_repr_masks_password(self):
        """13. WorkerConfig repr output redacts sensitive passwords."""
        cfg = WorkerConfig(saucedemo_password="super_secret_sauce_password")
        repr_str = repr(cfg)
        assert "super_secret_sauce_password" not in repr_str
        assert "[REDACTED]" in repr_str
