# -*- coding: utf-8 -*-
from odoo.tests.common import TransactionCase
from odoo.exceptions import AccessError


class TestIntegrationFoundation(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager_group = cls.env.ref('cs_integration_lab.group_integration_manager')
        cls.test_manager = cls.env['res.users'].create({
            'name': 'Test Integration Manager',
            'login': 'test_int_manager',
            'email': 'int_manager@example.com',
            'group_ids': [(6, 0, [cls.manager_group.id, cls.env.ref('base.group_user').id])]
        })
        cls.test_user = cls.env['res.users'].create({
            'name': 'Test Normal User',
            'login': 'test_norm_user',
            'email': 'norm_user@example.com',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])]
        })

    def test_01_integration_config_creation(self):
        """Test creating integration configuration records."""
        config = self.env['cs.integration.config'].create({
            'name': 'Test GitHub Integration',
            'provider': 'github',
            'schedule_enabled': True,
        })
        self.assertEqual(config.name, 'Test GitHub Integration')
        self.assertEqual(config.provider, 'github')
        self.assertTrue(config.active)
        self.assertTrue(config.schedule_enabled)

    def test_02_sync_log_creation(self):
        """Test creating sync log records programmatically."""
        log = self.env['cs.integration.sync.log'].create_log(
            provider='github',
            status='success',
            created_count=5,
            updated_count=2,
            skipped_count=0,
            failed_count=0
        )
        self.assertEqual(log.provider, 'github')
        self.assertEqual(log.status, 'success')
        self.assertEqual(log.created_count, 5)
        self.assertEqual(log.updated_count, 2)

    def test_03_sync_now_action(self):
        """Test that Sync Now action creates a log entry."""
        config = self.env['cs.integration.config'].with_user(self.test_manager).create({
            'name': 'Test Frankfurter Integration',
            'provider': 'frankfurter',
        })
        action = config.action_sync_now()
        self.assertIsNotNone(config.last_sync_at)
        log = self.env['cs.integration.sync.log'].search([('config_id', '=', config.id)], limit=1)
        self.assertTrue(log.exists())
        self.assertEqual(log.provider, 'frankfurter')

    def test_04_security_access(self):
        """Test security access restriction for normal users on Sync Now."""
        config = self.env['cs.integration.config'].with_user(self.test_manager).create({
            'name': 'Test Open-Meteo Integration',
            'provider': 'open_meteo',
        })
        with self.assertRaises(AccessError):
            config.with_user(self.test_user).action_sync_now()

    def test_05_idempotency_mixin(self):
        """Test idempotency mixin hash computation."""
        payload = {'id': 123, 'name': 'Sample Issue', 'status': 'open'}
        hash_val = self.env['cs.integration.idempotency.mixin'].compute_payload_hash(payload)
        self.assertTrue(isinstance(hash_val, str))
        self.assertEqual(len(hash_val), 64)  # SHA-256 length
