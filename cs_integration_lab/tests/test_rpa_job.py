# -*- coding: utf-8 -*-
from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError, ValidationError


class TestRpaJob(TransactionCase):

    def setUp(self):
        super(TestRpaJob, self).setUp()
        self.RpaJob = self.env['cs.rpa.job']
        self.valid_payload = '{"product_name": "Sauce Labs Backpack", "quantity": 1}'

    def test_01_create_without_job_type_rejected(self):
        """Creating an RPA job without job_type should be rejected."""
        with self.assertRaises(ValidationError):
            self.RpaJob.create({
                'job_type': False,
                'idempotency_key': 'KEY-TEST-01',
                'payload': self.valid_payload,
            })

    def test_02_create_with_empty_payload_rejected(self):
        """Creating an RPA job with empty payload should be rejected."""
        with self.assertRaises(ValidationError):
            self.RpaJob.create({
                'job_type': 'saucedemo',
                'idempotency_key': 'KEY-TEST-02',
                'payload': '',
            })

    def test_03_create_with_whitespace_payload_rejected(self):
        """Creating an RPA job with whitespace-only payload should be rejected."""
        with self.assertRaises(ValidationError):
            self.RpaJob.create({
                'job_type': 'saucedemo',
                'idempotency_key': 'KEY-TEST-03',
                'payload': '   \n  \t  ',
            })

    def test_04_create_with_invalid_json_payload_rejected(self):
        """Creating an RPA job with invalid JSON payload should be rejected."""
        with self.assertRaises(ValidationError):
            self.RpaJob.create({
                'job_type': 'saucedemo',
                'idempotency_key': 'KEY-TEST-04',
                'payload': '{bad_json: true}',
            })

    def test_05_create_without_idempotency_key_rejected(self):
        """Creating an RPA job without idempotency_key should be rejected."""
        with self.assertRaises(ValidationError):
            self.RpaJob.create({
                'job_type': 'saucedemo',
                'idempotency_key': False,
                'payload': self.valid_payload,
            })

    def test_06_create_with_whitespace_idempotency_key_rejected(self):
        """Creating an RPA job with whitespace-only idempotency_key should be rejected."""
        with self.assertRaises(ValidationError):
            self.RpaJob.create({
                'job_type': 'saucedemo',
                'idempotency_key': '   \t  ',
                'payload': self.valid_payload,
            })

    def test_07_duplicate_idempotency_key_rejected(self):
        """Duplicate non-empty idempotency keys must be rejected."""
        self.RpaJob.create({
            'job_type': 'saucedemo',
            'idempotency_key': 'KEY-DUP-01',
            'payload': self.valid_payload,
        })
        with self.assertRaises(ValidationError):
            self.RpaJob.create({
                'job_type': 'saucedemo',
                'idempotency_key': '  KEY-DUP-01  ',  # whitespace stripped, duplicate
                'payload': self.valid_payload,
            })

    def test_08_valid_job_creation_succeeds(self):
        """Valid job creation succeeds with default state 'draft' and attempt_count 0."""
        job = self.RpaJob.create({
            'job_type': 'saucedemo',
            'idempotency_key': 'KEY-VALID-01',
            'payload': self.valid_payload,
        })
        self.assertTrue(job.id)
        self.assertEqual(job.state, 'draft')
        self.assertEqual(job.attempt_count, 0)
        self.assertEqual(job.idempotency_key, 'KEY-VALID-01')

    def test_09_draft_to_queued_succeeds(self):
        """Draft job can be transitioned to queued state."""
        job = self.RpaJob.create({
            'job_type': 'saucedemo',
            'idempotency_key': 'KEY-TRANS-01',
            'payload': self.valid_payload,
        })
        job.action_queue()
        self.assertEqual(job.state, 'queued')

    def test_10_queued_to_draft_rejected(self):
        """State transition from queued -> draft must be rejected."""
        job = self.RpaJob.create({
            'job_type': 'saucedemo',
            'idempotency_key': 'KEY-TRANS-02',
            'payload': self.valid_payload,
            'state': 'queued',
        })
        with self.assertRaises(ValidationError):
            job.write({'state': 'draft'})

    def test_11_running_to_success_failed_needs_human_succeeds(self):
        """State transition running -> success/failed/needs_human must succeed."""
        job = self.RpaJob.create({
            'job_type': 'saucedemo',
            'idempotency_key': 'KEY-TRANS-03',
            'payload': self.valid_payload,
            'state': 'queued',
        })
        job.write({'state': 'running'})
        self.assertEqual(job.state, 'running')

        job.write({'state': 'success'})
        self.assertEqual(job.state, 'success')

        job2 = self.RpaJob.create({
            'job_type': 'saucedemo',
            'idempotency_key': 'KEY-TRANS-04',
            'payload': self.valid_payload,
            'state': 'queued',
        })
        job2.write({'state': 'running'})
        job2.write({'state': 'failed'})
        self.assertEqual(job2.state, 'failed')

        job3 = self.RpaJob.create({
            'job_type': 'saucedemo',
            'idempotency_key': 'KEY-TRANS-05',
            'payload': self.valid_payload,
            'state': 'queued',
        })
        job3.write({'state': 'running'})
        job3.write({'state': 'needs_human'})
        self.assertEqual(job3.state, 'needs_human')

    def test_12_success_to_queued_rejected(self):
        """Transitioning from terminal state 'success' to 'queued' must be rejected."""
        job = self.RpaJob.create({
            'job_type': 'saucedemo',
            'idempotency_key': 'KEY-TRANS-06',
            'payload': self.valid_payload,
            'state': 'queued',
        })
        job.write({'state': 'running'})
        job.write({'state': 'success'})
        with self.assertRaises(ValidationError):
            job.write({'state': 'queued'})

    def test_13_failed_to_queued_succeeds(self):
        """Failed job can be retried to queued state, incrementing attempt_count."""
        job = self.RpaJob.create({
            'job_type': 'saucedemo',
            'idempotency_key': 'KEY-TRANS-07',
            'payload': self.valid_payload,
            'state': 'queued',
        })
        job.write({'state': 'running'})
        job.write({'state': 'failed', 'error_details': 'Timeout'})
        self.assertEqual(job.attempt_count, 0)

        job.action_retry()
        self.assertEqual(job.state, 'queued')
        self.assertEqual(job.attempt_count, 1)
        self.assertFalse(job.error_details)

    def test_14_needs_human_to_queued_succeeds(self):
        """Needs_human job can be retried to queued state, incrementing attempt_count."""
        job = self.RpaJob.create({
            'job_type': 'saucedemo',
            'idempotency_key': 'KEY-TRANS-08',
            'payload': self.valid_payload,
            'state': 'queued',
        })
        job.write({'state': 'running'})
        job.write({'state': 'needs_human', 'error_details': 'CAPTCHA required'})

        job.action_retry()
        self.assertEqual(job.state, 'queued')
        self.assertEqual(job.attempt_count, 1)
        self.assertFalse(job.error_details)
