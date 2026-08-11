# -*- coding: utf-8 -*-
"""Offline Pytest unit tests for RPA Job state logic, payload validation, and client record operations.

Test Architecture & Boundaries Note:
- The cs_integration_lab/tests/test_rpa_job.py module contains full Odoo ORM TransactionCase tests,
  which require an active Odoo server runtime (Odoo ORM database context) to evaluate @api.constrains
  and SQL unique constraints.
- This file (tests/test_rpa_job_unit.py) exercises pure Python state transition logic, input validation rules,
  and FakeOdooClient integration in an offline, headless Pytest environment.
"""
import json
import pytest

from integration_service.idempotency import compute_source_hash, make_external_id


def validate_queue_request(payload: str, idempotency_key: str) -> dict:
    """Simulates the input payload and idempotency key validation rule enforced during action_queue()."""
    if not idempotency_key or not idempotency_key.strip():
        raise ValueError("Idempotency key must be provided when queueing job.")
    if not payload or not payload.strip():
        raise ValueError("Input payload cannot be empty.")
    try:
        return json.loads(payload)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Input payload must be valid JSON: {exc}")


def check_idempotency_uniqueness(existing_keys: list, new_key: str) -> str:
    """Simulates the server-side uniqueness enforcement rule."""
    if not new_key or not str(new_key).strip():
        raise ValueError("Idempotency key is required and cannot be blank.")
    clean = str(new_key).strip().lower()
    for k in existing_keys:
        if k and str(k).strip().lower() == clean:
            raise ValueError(f"Validation Error: Idempotency key '{new_key}' is already in use by another RPA job.")
    return str(new_key).strip()


def detect_existing_duplicates(records: list) -> list:
    """Safely inspects existing records for duplicate non-empty idempotency keys."""
    counts = {}
    dups = []
    for r in records:
        key = r.get("x_idempotency_key")
        if key and str(key).strip():
            clean = str(key).strip().lower()
            if clean in counts:
                counts[clean].append(r)
            else:
                counts[clean] = [r]
    for clean, rec_list in counts.items():
        if len(rec_list) > 1:
            dups.extend(rec_list)
    return dups


def check_state_transition(current_state: str, target_state: str) -> bool:
    """Simulates the state transition validator enforced on cs.rpa.job write()."""
    allowed_transitions = {
        'draft': ['queued'],
        'queued': ['running'],
        'running': ['success', 'failed', 'needs_human'],
        'failed': ['queued'],
        'needs_human': ['queued'],
        'success': [],
    }
    if current_state != target_state and target_state not in allowed_transitions.get(current_state, []):
        raise ValueError(f"Unsafe transition from '{current_state}' to '{target_state}'")
    return True


def simulate_retry(job_dict: dict) -> dict:
    """Simulates action_retry() logic on a job record dict."""
    if job_dict.get('state') not in ('failed', 'needs_human'):
        raise ValueError(f"Only failed or needs_human jobs can be retried, got '{job_dict.get('state')}'")
    job_dict['state'] = 'queued'
    job_dict['attempt_count'] = job_dict.get('attempt_count', 0) + 1
    job_dict['error_details'] = False
    return job_dict


class TestRpaJobUnit:
    """Offline unit tests for RPA Job state transitions, payload rules, and client operations."""

    def test_01_job_creation_and_default_state(self, odoo):
        """1. Verify job creation and default state handling using FakeOdooClient."""
        job_id = odoo.create_one("cs.rpa.job", {
            "name": "RPA/2026/00001",
            "job_type": "saucedemo",
            "payload": json.dumps({"product": "backpack"}),
            "state": "draft",
            "idempotency_key": "idemp-001",
            "attempt_count": 0,
        })
        assert job_id is not None
        records = odoo.search_read("cs.rpa.job", [["id", "=", job_id]])
        assert len(records) == 1
        rec = records[0]
        assert rec["state"] == "draft"
        assert rec["attempt_count"] == 0
        assert rec["idempotency_key"] == "idemp-001"

    def test_02_queue_transition_logic(self):
        """2. Valid queue state transition and payload parsing with non-empty idempotency key."""
        raw_payload = '{"product_name": "Sauce Labs Bike Light", "qty": 2}'
        parsed = validate_queue_request(raw_payload, idempotency_key="idemp-key-002")
        assert parsed["product_name"] == "Sauce Labs Bike Light"

        assert check_state_transition("draft", "queued") is True

    def test_03_invalid_queue_input(self):
        """3. Reject missing/blank idempotency key and empty or invalid JSON payload."""
        payload = '{"product": "Backpack"}'

        # Missing idempotency key
        with pytest.raises(ValueError, match="Idempotency key must be provided"):
            validate_queue_request(payload, idempotency_key=None)

        # Blank idempotency key
        with pytest.raises(ValueError, match="Idempotency key must be provided"):
            validate_queue_request(payload, idempotency_key="   ")

        # Empty payload
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_queue_request("   ", idempotency_key="idemp-key")

        # Invalid JSON
        with pytest.raises(ValueError, match="valid JSON"):
            validate_queue_request("{invalid_json_payload:", idempotency_key="idemp-key")

    def test_04_retry_from_failed(self):
        """4. Retry transition from failed state increases attempt count."""
        job = {
            "name": "RPA/2026/00004",
            "state": "failed",
            "attempt_count": 0,
            "error_details": "Timeout waiting for checkout button",
            "result": "Partial step 2",
        }
        updated = simulate_retry(job)
        assert updated["state"] == "queued"
        assert updated["attempt_count"] == 1
        assert updated["error_details"] is False
        # Verify execution history preservation
        assert updated["result"] == "Partial step 2"

    def test_05_retry_from_needs_human(self):
        """5. Retry transition from needs_human state."""
        job = {
            "name": "RPA/2026/00005",
            "state": "needs_human",
            "attempt_count": 1,
            "error_details": "CAPTCHA challenge encountered",
        }
        updated = simulate_retry(job)
        assert updated["state"] == "queued"
        assert updated["attempt_count"] == 2
        assert updated["error_details"] is False

    def test_06_invalid_unsafe_state_transitions(self):
        """6. Prevent unsafe/arbitrary state transitions."""
        # Cannot jump from draft to success directly
        with pytest.raises(ValueError, match="Unsafe transition"):
            check_state_transition("draft", "success")

        # Cannot transition queued -> draft
        with pytest.raises(ValueError, match="Unsafe transition"):
            check_state_transition("queued", "draft")

        # Cannot jump from queued to success directly without running
        with pytest.raises(ValueError, match="Unsafe transition"):
            check_state_transition("queued", "success")

        # Success is a terminal state
        with pytest.raises(ValueError, match="Unsafe transition"):
            check_state_transition("success", "queued")

        # Cannot retry a draft job
        with pytest.raises(ValueError, match="Only failed or needs_human"):
            simulate_retry({"state": "draft"})

    def test_07_idempotency_key_helpers(self):
        """7. Test idempotency key generation and payload hashing helpers."""
        external_id = make_external_id("rpa", "saucedemo", "job-1001")
        assert external_id == "rpa:saucedemo:job-1001"

        payload1 = {"product": "Backpack", "user": "standard_user"}
        payload2 = {"user": "standard_user", "product": "Backpack"}
        # Compute hashes
        hash1 = compute_source_hash(payload1)
        hash2 = compute_source_hash(payload2)
        assert hash1 == hash2

    def test_08_attempt_count_accumulation(self):
        """8. Attempt count accumulates across multiple failed retry attempts."""
        job = {"state": "failed", "attempt_count": 0}
        job = simulate_retry(job)
        assert job["attempt_count"] == 1

        job["state"] = "needs_human"
        job = simulate_retry(job)
        assert job["attempt_count"] == 2

    def test_09_idempotency_uniqueness_suite(self):
        """9. Test complete idempotency uniqueness rules: first succeeds, same key rejected, different key succeeds, blank/whitespace rejected, existing duplicate detection."""
        existing_keys = []

        # First key succeeds
        k1 = check_idempotency_uniqueness(existing_keys, "rpa-unique-101")
        assert k1 == "rpa-unique-101"
        existing_keys.append("rpa-unique-101")

        # Same key again is rejected
        with pytest.raises(ValueError, match="already in use"):
            check_idempotency_uniqueness(existing_keys, "rpa-unique-101")

        # Same key with whitespace/different case is rejected
        with pytest.raises(ValueError, match="already in use"):
            check_idempotency_uniqueness(existing_keys, "  RPA-UNIQUE-101  ")

        # Different key succeeds
        k2 = check_idempotency_uniqueness(existing_keys, "rpa-unique-102")
        assert k2 == "rpa-unique-102"
        existing_keys.append("rpa-unique-102")

        # Blank key is rejected
        with pytest.raises(ValueError, match="required and cannot be blank"):
            check_idempotency_uniqueness(existing_keys, None)

        # Whitespace-only key is rejected
        with pytest.raises(ValueError, match="required and cannot be blank"):
            check_idempotency_uniqueness(existing_keys, "   \t  ")

        # Existing duplicate records detected safely
        mock_db_records = [
            {"id": 1, "x_idempotency_key": "key-a", "x_state": "queued"},
            {"id": 2, "x_idempotency_key": "key-b", "x_state": "draft"},
            {"id": 6, "x_idempotency_key": "rpa-manual-test-001", "x_state": "queued"},
            {"id": 7, "x_idempotency_key": "rpa-manual-test-001", "x_state": "queued"},
        ]
        duplicates = detect_existing_duplicates(mock_db_records)
        assert len(duplicates) == 2
        dup_ids = [d["id"] for d in duplicates]
        assert 6 in dup_ids and 7 in dup_ids
