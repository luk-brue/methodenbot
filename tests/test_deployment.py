import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch


MANAGER_PATH = Path(__file__).resolve().parents[1] / 'deployment/manage.py'
SPEC = importlib.util.spec_from_file_location('deployment_manager', MANAGER_PATH)
manager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manager)


class DeploymentTests(unittest.TestCase):
    def runtime_source(self, folder):
        values = {
            'UK_NUMMER': 'uk', 'EMAIL_ADDRESS': 'mail', 'EMAIL_PASSWORD': 'secret',
            'EWS_ENDPOINT': 'https://ews.invalid', 'MATRIX_SERVER': 'https://matrix.invalid',
            'MATRIX_USER': '@bot:x', 'MATRIX_PASSWORD': 'secret',
            'MATRIX_ROOM_ID': '!prod:x', 'MATRIX_CONSOLE_ROOM_ID': '!control:x',
            'MATRIX_CONTROL_USER': '@controller:matrix.invalid',
            'GOOGLE_FORM_LINK': 'https://form.invalid?',
            'GWDG_API_KEY': 'MUST-NOT-SURVIVE', 'METHODENBOT_EXPERIMENT_LIVE': 'true'}
        path = Path(folder) / '.env'
        path.write_text('\n'.join(key + '=' + value for key, value in values.items()) + '\n')
        return path

    def test_runtime_transform_preserves_service_secrets_but_removes_direct_ai_key(self):
        with tempfile.TemporaryDirectory() as folder:
            result = manager.final_runtime_env(self.runtime_source(folder)).decode()
        self.assertIn('EMAIL_PASSWORD=secret', result)
        self.assertNotIn('MUST-NOT-SURVIVE', result)
        self.assertNotIn('METHODENBOT_EXPERIMENT_LIVE', result)
        self.assertIn('MATRIX_ALLOW_UNENCRYPTED_CONTROL_DM=true', result)
        self.assertIn('METHODENBOT_AI_DEFAULT_ENABLED=false', result)
        self.assertIn('MATRIX_CONTROL_USER=@controller:matrix.invalid', result)
        self.assertIn('MATRIX_ADDITIONAL_CONTROL_ROOMS_JSON={}', result)

    def test_runtime_transform_canonicalizes_additional_control_rooms(self):
        with tempfile.TemporaryDirectory() as folder:
            source = self.runtime_source(folder)
            source.write_text(source.read_text() +
                              'MATRIX_ADDITIONAL_CONTROL_ROOMS_JSON=' +
                              '{"@zeta:example.invalid":"!zeta:example.invalid",'
                              '"@alpha:example.invalid":"!alpha:example.invalid"}\n')
            result = manager.final_runtime_env(source).decode()
        self.assertIn(
            'MATRIX_ADDITIONAL_CONTROL_ROOMS_JSON='
            '{"@alpha:example.invalid":"!alpha:example.invalid",'
            '"@zeta:example.invalid":"!zeta:example.invalid"}', result)

    def test_runtime_transform_rejects_invalid_additional_control_rooms(self):
        too_many = {f'@user{index}:example.invalid': f'!room{index}:example.invalid'
                    for index in range(9)}
        cases = (
            '[]',
            '{',
            '{"not-a-user":"!room:example.invalid"}',
            '{"@user:example.invalid":"not-a-room"}',
            ('{"@same:example.invalid":"!one:example.invalid",'
             '"@same:example.invalid":"!two:example.invalid"}'),
            ('{"@one:example.invalid":"!same:example.invalid",'
             '"@two:example.invalid":"!same:example.invalid"}'),
            '{"@controller:matrix.invalid":"!other:example.invalid"}',
            '{"@other:example.invalid":"!control:x"}',
            '{"@other:example.invalid":"!prod:x"}',
            json.dumps(too_many, separators=(',', ':')),
        )
        for index, raw in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as folder:
                source = self.runtime_source(folder)
                source.write_text(source.read_text() +
                                  'MATRIX_ADDITIONAL_CONTROL_ROOMS_JSON=' + raw + '\n')
                with self.assertRaisesRegex(
                        manager.DeployError, 'matrix_additional_control_rooms_invalid'):
                    manager.final_runtime_env(source)

    def test_runtime_transform_requires_explicit_control_user(self):
        with tempfile.TemporaryDirectory() as folder:
            source = self.runtime_source(folder)
            source.write_text('\n'.join(
                line for line in source.read_text().splitlines()
                if not line.startswith('MATRIX_CONTROL_USER=')) + '\n')
            with self.assertRaisesRegex(manager.DeployError, 'matrix_control_user_invalid'):
                manager.final_runtime_env(source)

    def test_gateway_parser_accepts_only_two_fixed_local_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'client.env'
            path.write_text('OPENAI_COMPATIBLE_BASE_URL=http://127.0.0.1:18765/v1\n'
                            'OPENAI_COMPATIBLE_API_KEY=SYNTHETIC\n')
            with patch.object(manager, 'GATEWAY_CLIENT', path):
                self.assertEqual(manager.gateway_token(), b'SYNTHETIC\n')
                path.write_text(path.read_text() + 'EXTRA=value\n')
                with self.assertRaisesRegex(manager.DeployError, 'gateway_client_invalid'):
                    manager.gateway_token()

    def test_manifest_ignores_generated_bytecode_but_not_unlisted_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'a.txt').write_text('a')
            digest = hashlib.sha256(b'a').hexdigest()
            manifest = root / 'MANIFEST.sha256'
            manifest.write_text(digest + '  a.txt\n')
            cache = root / 'deployment/__pycache__'
            cache.mkdir(parents=True)
            (cache / 'manage.pyc').write_bytes(b'generated')
            with patch.object(manager, 'ROOT', root), patch.object(manager, 'MANIFEST', manifest):
                manager.verify_bundle()
                (root / 'unexpected.py').write_text('x')
                with self.assertRaisesRegex(manager.DeployError, 'bundle_members_differ'):
                    manager.verify_bundle()

    def test_dropin_uses_external_state_config_and_systemd_credential(self):
        text = manager.dropin_text().decode()
        self.assertIn('METHODENBOT_ENV_FILE=/etc/methodenbot/runtime.env', text)
        self.assertIn('METHODENBOT_STATE_DIR=/var/lib/methodenbot', text)
        self.assertIn('LoadCredential=gwdg-local-token:', text)
        self.assertIn('UMask=0077', text)

    def test_first_install_initializes_runtime_from_legacy_env_and_gateway(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old, etc = root / 'old', root / 'etc'
            old.mkdir()
            self.runtime_source(old)
            runtime, token = etc / 'runtime.env', etc / 'gwdg-local-token'
            identity = SimpleNamespace(pw_gid=os.getgid())
            with (patch.object(manager, 'OLD', old), patch.object(manager, 'ETC', etc),
                  patch.object(manager, 'RUNTIME_ENV', runtime),
                  patch.object(manager, 'LOCAL_TOKEN', token),
                  patch.object(manager, 'installed_final_release', return_value=None),
                  patch.object(manager, 'gateway_token', return_value=b'LOCAL-ONLY\n') as gateway,
                  patch.object(manager, 'atomic_write') as write,
                  patch.object(manager.os, 'chown')):
                self.assertIsNone(manager.prepare_runtime_configuration(identity))
            self.assertEqual(write.call_count, 2)
            self.assertEqual(write.call_args_list[0].args[0], runtime)
            self.assertIn(b'EMAIL_PASSWORD=secret', write.call_args_list[0].args[1])
            self.assertEqual(write.call_args_list[1].args[:2], (token, b'LOCAL-ONLY\n'))
            gateway.assert_called_once_with()

    def test_follow_release_preserves_canonical_runtime_and_local_token(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old, etc = root / 'old', root / 'etc'
            old.mkdir()
            self.runtime_source(old)
            etc.mkdir()
            runtime = etc / 'runtime.env'
            runtime.write_bytes(manager.final_runtime_env(self.runtime_source(etc)))
            # This value represents a credential rotation made after first deploy.
            runtime.write_text(runtime.read_text().replace('EMAIL_PASSWORD=secret',
                                                           'EMAIL_PASSWORD=rotated'))
            token = etc / 'gwdg-local-token'
            token.write_text('ROTATED-LOCAL-TOKEN\n')
            before_runtime, before_token = runtime.read_bytes(), token.read_bytes()
            identity = SimpleNamespace(pw_gid=os.getgid())
            release = Path('/srv/methodenbot-final/releases/final-one')
            with (patch.object(manager, 'OLD', old), patch.object(manager, 'ETC', etc),
                  patch.object(manager, 'RUNTIME_ENV', runtime),
                  patch.object(manager, 'LOCAL_TOKEN', token),
                  patch.object(manager, 'installed_final_release', return_value=release),
                  patch.object(manager, 'validate_protected_file'),
                  patch.object(manager, 'gateway_token') as gateway,
                  patch.object(manager, 'atomic_write') as write,
                  patch.object(manager.os, 'chown')):
                self.assertEqual(manager.prepare_runtime_configuration(identity), release)
            self.assertEqual(runtime.read_bytes(), before_runtime)
            self.assertEqual(token.read_bytes(), before_token)
            write.assert_not_called()
            gateway.assert_not_called()

    def test_follow_release_rejects_direct_key_in_canonical_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime, token = root / 'runtime.env', root / 'gwdg-local-token'
            source = self.runtime_source(root)
            runtime.write_bytes(manager.final_runtime_env(source))
            runtime.write_text(runtime.read_text() + 'GWDG_API_KEY=forbidden\n')
            runtime.chmod(0o640)
            token.write_text('LOCAL-ONLY\n')
            token.chmod(0o600)
            identity = SimpleNamespace(pw_gid=os.getgid())
            with (patch.object(manager, 'RUNTIME_ENV', runtime),
                  patch.object(manager, 'LOCAL_TOKEN', token),
                  patch.object(manager, 'validate_protected_file')):
                with self.assertRaisesRegex(
                        manager.DeployError, 'canonical_runtime_configuration_invalid'):
                    manager.validate_existing_runtime(identity)

    def test_service_verification_rejects_restart_during_stability_window(self):
        with patch.object(manager, 'service_snapshot', side_effect=[101, 202]):
            with self.assertRaisesRegex(manager.DeployError, 'service_not_stable'):
                manager.verify_service(Path('/srv/methodenbot-final/releases/final-one'),
                                       attempts=1, stable_seconds=0, sleep=lambda _seconds: None)

    def test_control_bootstrap_waits_for_cursor_without_accepting_pid_change(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            control = state / 'control/state.json'
            control.parent.mkdir(mode=0o700)
            ready = state / 'control/ready.json'
            runtime = state / 'runtime.env'
            runtime.write_text('MATRIX_CONTROL_USER=@controller:matrix.invalid\n'
                               'MATRIX_CONSOLE_ROOM_ID=!control:x\n'
                               'MATRIX_ROOM_ID=!prod:x\n'
                               'MATRIX_ADDITIONAL_CONTROL_ROOMS_JSON={}\n')
            identity = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
            waits = []

            def finish_bootstrap(seconds):
                waits.append(seconds)
                control.write_text('{"since":"cursor","ai_enabled":false}\n')
                control.chmod(0o600)
                ready.write_text(json.dumps({
                    'version': 1, 'pid': 123,
                    'controllers_sha256': manager.control_bindings_hash(
                        '@controller:matrix.invalid', '!control:x', {})}) + '\n')
                ready.chmod(0o600)

            release = Path('/srv/methodenbot-final/releases/final-one')
            with (patch.object(manager, 'STATE', state),
                  patch.object(manager, 'RUNTIME_ENV', runtime),
                  patch.object(manager, 'service_snapshot', return_value=123)):
                result = manager.wait_control_ready(
                    release, 123, identity, attempts=2, sleep=finish_bootstrap)
            self.assertEqual(result['since'], 'cursor')
            self.assertEqual(waits, [1])

            with (patch.object(manager, 'RUNTIME_ENV', runtime),
                  patch.object(manager, 'service_snapshot', return_value=124)):
                with self.assertRaisesRegex(manager.DeployError, 'service_not_stable'):
                    manager.wait_control_ready(release, 123, identity, attempts=1,
                                               sleep=lambda _seconds: None)

    def test_control_readiness_rejects_marker_for_other_controller_mapping(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            control_dir = state / 'control'
            control_dir.mkdir(mode=0o700)
            control = control_dir / 'state.json'
            control.write_text('{"since":"cursor","ai_enabled":false}\n')
            control.chmod(0o600)
            ready = control_dir / 'ready.json'
            ready.write_text(json.dumps({
                'version': 1, 'pid': 123, 'controllers_sha256': '0' * 64}) + '\n')
            ready.chmod(0o600)
            runtime = state / 'runtime.env'
            runtime.write_text('MATRIX_CONTROL_USER=@controller:matrix.invalid\n'
                               'MATRIX_CONSOLE_ROOM_ID=!control:x\n'
                               'MATRIX_ROOM_ID=!prod:x\n'
                               'MATRIX_ADDITIONAL_CONTROL_ROOMS_JSON={}\n')
            identity = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
            with (patch.object(manager, 'STATE', state),
                  patch.object(manager, 'RUNTIME_ENV', runtime),
                  patch.object(manager, 'service_snapshot', return_value=123)):
                with self.assertRaisesRegex(manager.DeployError, 'control_state_invalid'):
                    manager.wait_control_ready(
                        Path('/srv/methodenbot-final/releases/final-one'), 123, identity,
                        attempts=1, sleep=lambda _seconds: None)

    def test_restore_failure_is_explicit_and_leaves_service_stopped(self):
        restore = Mock()
        with (patch.object(manager, 'run') as run,
              patch.object(manager, 'verify_service', side_effect=manager.DeployError('bad'))):
            with self.assertRaisesRegex(
                    manager.DeployError, 'automatic_restore_failed_service_left_stopped'):
                manager.restore_and_verify(None, restore,
                                           'automatic_restore_failed_service_left_stopped')
        restore.assert_called_once_with()
        self.assertEqual(run.call_args_list[-1],
                         call(['/usr/bin/systemctl', 'stop', manager.SERVICE], check=False))

    def test_rollback_refuses_wrong_lineage_before_service_or_csv_change(self):
        name = 'before-final-20260831T100000Z'
        with tempfile.TemporaryDirectory() as folder:
            backup = Path(folder) / name
            backup.mkdir()
            (backup / 'metadata.json').write_text('{}')
            activated = Path('/srv/methodenbot-final/releases/final-two')
            prior = Path('/srv/methodenbot-final/releases/final-one')
            with (patch.object(manager, 'BACKUPS', Path(folder)),
                  patch.object(manager, 'require_root'),
                  patch.object(manager, 'service_identity'),
                  patch.object(manager, 'backup_metadata', return_value={
                      'activated_release': activated, 'prior_current': prior}),
                  patch.object(manager, 'installed_final_release', return_value=prior),
                  patch.object(manager, 'verify_service') as verify,
                  patch.object(manager, 'replace_runtime_file') as replace,
                  patch.object(manager, 'run') as run):
                with self.assertRaisesRegex(manager.DeployError, 'rollback_lineage_mismatch'):
                    manager.rollback(name, True)
            verify.assert_not_called()
            replace.assert_not_called()
            run.assert_not_called()

    def test_rollback_requires_stable_active_final_before_csv_change(self):
        name = 'before-final-20260831T100001Z'
        with tempfile.TemporaryDirectory() as folder:
            backup = Path(folder) / name
            backup.mkdir()
            (backup / 'metadata.json').write_text('{}')
            activated = Path('/srv/methodenbot-final/releases/final-one')
            with (patch.object(manager, 'BACKUPS', Path(folder)),
                  patch.object(manager, 'require_root'),
                  patch.object(manager, 'service_identity'),
                  patch.object(manager, 'backup_metadata', return_value={
                      'activated_release': activated, 'prior_current': None}),
                  patch.object(manager, 'installed_final_release', return_value=activated),
                  patch.object(manager, 'verify_service',
                               side_effect=manager.DeployError('service_verification_failed')),
                  patch.object(manager, 'replace_runtime_file') as replace,
                  patch.object(manager, 'run') as run):
                with self.assertRaisesRegex(manager.DeployError, 'service_verification_failed'):
                    manager.rollback(name, True)
            replace.assert_not_called()
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
