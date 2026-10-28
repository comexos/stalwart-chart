"""Reconciliation state-machine checks without a cluster or a Stalwart server."""
import base64
import copy
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('reconcile', Path(__file__).resolve().parents[1] / 'files/reconcile.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class ReconcileChecks(unittest.TestCase):
    def setUp(self):
        self.config = {'release': 'mail', 'lockTimeoutSeconds': 300,
                       'restart': {'namespace': 'mail', 'statefulset': 'mail', 'settingsHash': 'hash', 'timeoutSeconds': 30}}
        self.account = {'id': 'mailer', 'name': 'mailer', 'address': 'mailer@example.org',
                        'secret': {'namespace': 'mail', 'name': 'smtp', 'keys': ['password', 'smtpPassword']},
                        'appPassword': {'allowedIps': ['10.0.0.0/8'], 'permissions': ['authenticate', 'emailSend']}}
        self.secret = {'metadata': {'namespace': 'mail', 'name': 'smtp', 'resourceVersion': '1',
                                    'annotations': {r.CREDENTIAL: 'ref'}},
                       'data': {'password': base64.b64encode(b'old').decode(), 'unrelated': 'a2VlcA=='}}
        self.kube = Mock()
        self.kube.get_secret.side_effect = lambda *a: copy.deepcopy(self.secret)
        def replace(secret):
            self.secret = copy.deepcopy(secret)
            return copy.deepcopy(secret)
        self.kube.replace_secret.side_effect = replace
        self.stalwart = Mock()
        self.stalwart.account_id.return_value = 'account'
        self.password = {'id': 'old-id', 'description': 'managed:mail/mailer #ref',
                         'allowedIps': {'10.0.0.0/8': True}, 'permissions': ['authenticate', 'emailSend']}
        self.stalwart.app_passwords.return_value = [self.password]
        self.stalwart.create_app_password.return_value = ('new-id', 'new-password')
        self.stalwart.set_settings_marker.return_value = 'role'
        self.ss = {'metadata': {'generation': 2}, 'spec': {'replicas': 1, 'template': {'metadata': {'annotations': {r.SETTINGS: 'hash@1'}}}},
                   'status': {'observedGeneration': 2, 'updatedReplicas': 1, 'readyReplicas': 1, 'currentRevision': 'v2', 'updateRevision': 'v2'}}
        self.kube.get_statefulset.return_value = self.ss

    def reconcile(self):
        r.reconcile_account(self.config, self.account, self.kube, self.stalwart)

    def test_existing_password_syncs_keys_without_rotation(self):
        self.reconcile()
        self.assertEqual(self.secret['data']['smtpPassword'], self.secret['data']['password'])
        self.assertEqual(self.secret['data']['unrelated'], 'a2VlcA==')
        self.assertNotIn(r.LOCK, self.secret['metadata']['annotations'])
        self.stalwart.create_app_password.assert_not_called()
        self.stalwart.update_app_password.assert_not_called()
        self.stalwart.destroy_app_passwords.assert_not_called()

    def test_restrictions_change_without_rotation(self):
        self.account['appPassword']['allowedIps'] = ['fd00::/8']
        self.reconcile()
        self.stalwart.update_app_password.assert_called_once_with('account', 'old-id', {'allowedIps': {'fd00::/8': True}, 'permissions': ['authenticate', 'emailSend']})
        self.stalwart.create_app_password.assert_not_called()

    def test_database_reset_creates_and_publishes_password(self):
        self.stalwart.app_passwords.return_value = []
        self.reconcile()
        self.stalwart.create_app_password.assert_called_once()
        self.assertEqual(base64.b64decode(self.secret['data']['password']), b'new-password')
        self.assertNotEqual(self.secret['metadata']['annotations'][r.CREDENTIAL], 'ref')

    def test_lost_secret_data_revokes_unrecoverable_password(self):
        self.secret['data'] = {}
        self.reconcile()
        self.stalwart.destroy_app_passwords.assert_called_once_with('account', ['old-id'])
        self.stalwart.create_app_password.assert_called_once()

    def test_orphans_are_revoked_but_foreign_credentials_are_preserved(self):
        self.stalwart.app_passwords.return_value += [
            {'id': 'orphan', 'description': 'managed:mail/mailer #lost'},
            {'id': 'legacy', 'description': 'managed:mail/mailer'},
            {'id': 'foreign', 'description': 'managed:other/mailer #foreign'}]
        self.reconcile()
        self.stalwart.destroy_app_passwords.assert_called_once_with('account', ['legacy', 'orphan'])
        self.stalwart.create_app_password.assert_not_called()

    def test_admin_api_failure_does_not_rotate(self):
        self.stalwart.app_passwords.side_effect = r.Failure('unreachable')
        with self.assertRaises(r.Failure):
            self.reconcile()
        self.stalwart.create_app_password.assert_not_called()
        self.stalwart.destroy_app_passwords.assert_not_called()
        self.assertNotIn(r.LOCK, self.secret['metadata']['annotations'])

    def test_failed_smtp_check_does_not_replace_existing_password(self):
        self.config['smtpCheck'] = {'host': 'smtp', 'port': 2525}
        with patch.object(r, 'smtp_check', side_effect=r.Failure('login failed')), self.assertRaises(r.Failure):
            self.reconcile()
        self.stalwart.create_app_password.assert_not_called()
        self.stalwart.destroy_app_passwords.assert_not_called()
        self.assertEqual(self.secret['metadata']['annotations'][r.CREDENTIAL], 'ref')

    def test_failed_new_password_check_revokes_without_publishing(self):
        self.stalwart.app_passwords.return_value = []
        self.config['smtpCheck'] = {'host': 'smtp', 'port': 2525}
        with patch.object(r, 'smtp_check', side_effect=r.Failure('login failed')), self.assertRaises(r.Failure):
            self.reconcile()
        self.stalwart.destroy_app_passwords.assert_called_once_with('account', ['new-id'])
        self.assertEqual(base64.b64decode(self.secret['data']['password']), b'old')

    def test_failed_publication_is_recovered_as_an_orphan(self):
        self.stalwart.app_passwords.return_value = []
        replace = self.kube.replace_secret.side_effect
        writes = 0
        def fail_publication(secret):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise r.Failure('conflict during publication')
            return replace(secret)
        self.kube.replace_secret.side_effect = fail_publication
        with self.assertRaises(r.Failure):
            self.reconcile()
        created = self.stalwart.create_app_password.call_args.args[1]
        self.assertEqual(self.secret['metadata']['annotations'][r.CREDENTIAL], 'ref')
        self.stalwart.app_passwords.return_value = [{'id': 'new-id', **created}]
        self.stalwart.create_app_password.return_value = ('replacement-id', 'replacement-password')
        self.reconcile()
        self.stalwart.destroy_app_passwords.assert_called_once_with('account', ['new-id'])
        self.assertEqual(base64.b64decode(self.secret['data']['password']), b'replacement-password')

    def test_active_lock_blocks_another_job(self):
        self.secret['metadata']['annotations'][r.LOCK] = 'other@100'
        with patch.dict(os.environ, {'JOB_UID': 'this'}), patch.object(r.time, 'time', return_value=101), self.assertRaises(r.Failure):
            self.reconcile()
        self.kube.replace_secret.assert_not_called()
        self.stalwart.account_id.assert_not_called()

    def test_conflict_on_lock_does_not_touch_stalwart(self):
        self.kube.replace_secret.side_effect = r.Failure('conflict')
        with self.assertRaises(r.Failure):
            self.reconcile()
        self.stalwart.account_id.assert_not_called()

    def test_kubernetes_conflict_is_reported(self):
        kube = r.Kubernetes.__new__(r.Kubernetes)
        kube.request = Mock(return_value=(409, {}))
        with self.assertRaisesRegex(r.Failure, 'changed concurrently'):
            kube.replace_secret(self.secret)

    def test_unchanged_settings_skip_restart(self):
        self.stalwart.settings_marker.return_value = ('role', 'done', 'hash@1')
        r.restart(self.config, self.kube, self.stalwart)
        self.kube.patch_statefulset.assert_not_called()
        self.stalwart.set_settings_marker.assert_not_called()

    def test_pending_restart_resumes_same_stamp(self):
        for applied in ('hash@1', 'old@0'):
            with self.subTest(applied=applied):
                self.kube.reset_mock()
                self.ss['spec']['template']['metadata']['annotations'][r.SETTINGS] = applied
                self.stalwart.settings_marker.return_value = ('role', 'pending', 'hash@1')
                r.restart(self.config, self.kube, self.stalwart)
                self.assertEqual(self.kube.patch_statefulset.call_count, int(applied != 'hash@1'))
                self.stalwart.set_settings_marker.assert_called_with('mail', 'role', 'done', 'hash@1')

    def test_database_reset_restarts_unchanged_settings(self):
        self.stalwart.settings_marker.return_value = (None, None, None)
        with patch.object(r.time, 'time', return_value=100):
            r.restart(self.config, self.kube, self.stalwart)
        self.kube.patch_statefulset.assert_called_once()
        self.assertEqual([c.args[2:] for c in self.stalwart.set_settings_marker.call_args_list], [('pending', 'hash@100'), ('done', 'hash@100')])

    def test_rollout_timeout_does_not_mark_done(self):
        self.ss['status']['readyReplicas'] = 0
        self.stalwart.settings_marker.return_value = ('role', 'pending', 'hash@1')
        with patch.object(r.time, 'time', side_effect=[0, 31]), self.assertRaises(r.Failure):
            r.restart(self.config, self.kube, self.stalwart)
        self.stalwart.set_settings_marker.assert_not_called()

    def test_rollout_requires_observed_generation_and_matching_revisions(self):
        self.assertTrue(r.rolled_out(self.ss))
        for key, value in [('observedGeneration', 1), ('updatedReplicas', 0), ('readyReplicas', 0), ('currentRevision', 'old')]:
            ss = copy.deepcopy(self.ss)
            ss['status'][key] = value
            self.assertFalse(r.rolled_out(ss), key)

    def test_ipv6_api_url(self):
        with patch.dict(os.environ, {'KUBERNETES_SERVICE_HOST': 'fd00::1', 'KUBERNETES_SERVICE_PORT': '443', 'KUBERNETES_API': ''}), patch.object(r.ssl, 'create_default_context'):
            self.assertEqual(r.Kubernetes().base, 'https://[fd00::1]:443')


if __name__ == '__main__':
    unittest.main()
