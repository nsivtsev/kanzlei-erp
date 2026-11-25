import importlib.util
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / 'deploy' / 'manage.py'
spec = importlib.util.spec_from_file_location('ai_deploy_manage', SOURCE)
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


class AIConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'shared').mkdir()
        self.path = self.root / 'shared' / 'production.env'
        self.base = ('DOMAIN=erp.example.com\nSITE_NAME=erp.example.com\n'
                     'ACME_EMAIL=ops@example.com\nDB_PASSWORD=secret\nADMIN_PASSWORD=secret\n')

    def config(self, extra=''):
        self.path.write_text(self.base + extra)
        self.path.chmod(0o600)
        return manager.read_config(self.root)

    def test_disabled_by_default(self):
        self.assertEqual(self.config()['AI_ENABLED'], '0')

    def test_enabled_requires_secrets_and_immutable_images(self):
        with self.assertRaisesRegex(ValueError, 'AI_'):
            self.config('AI_ENABLED=1\n')

    def test_enabled_accepts_complete_configuration(self):
        digest = 'a' * 64
        result = self.config('AI_ENABLED=1\nAI_API_IMAGE=registry.example/api@sha256:' + digest +
            '\nAI_WORKER_IMAGE=registry.example/worker@sha256:' + digest +
            '\nAI_POSTGRES_PASSWORD=' + 'b' * 32 + '\nAI_ERP_SERVICE_KEY=' + 'c' * 32 + '\n')
        self.assertEqual(result['AI_ENABLED'], '1')

    def test_enabled_compose_stops_consumers_before_migration(self):
        digest = 'a' * 64
        config = self.config('AI_ENABLED=1\nAI_API_IMAGE=registry.example/api@sha256:' + digest +
            '\nAI_WORKER_IMAGE=registry.example/worker@sha256:' + digest +
            '\nAI_POSTGRES_PASSWORD=' + 'b' * 32 + '\nAI_ERP_SERVICE_KEY=' + 'c' * 32 + '\n')
        release = self.root / 'release'
        (release / 'deploy').mkdir(parents=True)
        (release / 'deploy' / 'compose.yml').write_text('services:\n  ai-api:\n')
        class Commands:
            def __init__(self):
                self.calls = []

            def run(self, args, **kwargs):
                self.calls.append(args)
                return ''
        commands = Commands()
        compose = manager.Compose(self.root, release, 'image', commands, config)
        compose.stop()
        compose.ai_migrate()
        stop = next(i for i, call in enumerate(commands.calls) if 'stop' in call and 'ai-api' in call)
        migrate = next(i for i, call in enumerate(commands.calls) if 'migrate' in call)
        self.assertLess(stop, migrate)

    def test_restore_disables_ai_without_removing_saved_credentials(self):
        self.path.write_text(self.base + 'AI_ENABLED=1\nAI_POSTGRES_PASSWORD=keepme\n')
        self.path.chmod(0o600)
        manager.disable_ai_after_restore(self.path, {"AI_POSTGRES_PASSWORD": "new-value", "AI_API_IMAGE": "current-image"})
        result = self.path.read_text()
        self.assertIn('AI_ENABLED=0\n', result)
        self.assertIn('AI_POSTGRES_PASSWORD=new-value\n', result)
        self.assertIn('AI_API_IMAGE=current-image\n', result)
        self.assertNotIn('keepme', result)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_command_errors_redact_service_keys(self):
        import sys
        with self.assertRaises(RuntimeError) as error:
            manager.Commands().run([sys.executable, '-c', 'import os,sys; print(os.environ["AI_ERP_SERVICE_KEY"]); sys.exit(1)'],
                                   env={'AI_ERP_SERVICE_KEY': 'private-key-value'})
        self.assertNotIn('private-key-value', str(error.exception))

    def test_restore_stops_ai_from_interrupted_candidate(self):
        import json
        release = self.root / 'candidate'
        (release / 'deploy').mkdir(parents=True)
        (release / 'deploy' / 'compose.yml').write_text('services:\n  ai-api:\n')
        (self.root / 'shared/pending.json').write_text(json.dumps({'release': str(release), 'image': 'candidate'}))
        class Commands:
            def __init__(self):
                self.calls = []
            def run(self, args, **kwargs):
                self.calls.append(args)
                return ''
        commands = Commands()
        manager.stop_ai_for_restore(self.root, commands, self.config())
        self.assertTrue(any('stop' in call and 'ai-api' in call for call in commands.calls))

    def test_reconcile_uses_current_release_and_migrates_before_starting_consumers(self):
        import json
        digest = 'a' * 64
        self.config('AI_ENABLED=1\nAI_API_IMAGE=registry.example/api@sha256:' + digest +
                    '\nAI_WORKER_IMAGE=registry.example/worker@sha256:' + digest +
                    '\nAI_POSTGRES_PASSWORD=' + 'b' * 32 + '\nAI_ERP_SERVICE_KEY=' + 'c' * 32 + '\n')
        release = self.root / 'release'
        (release / 'deploy').mkdir(parents=True)
        (release / 'deploy/compose.yml').write_text('services:\n  ai-api:\n')
        state = {'release': str(release), 'image': 'ghcr.io/nsivtsev/kanzlei-erp@sha256:' + digest,
                 'revision': 'b' * 40, 'tag': 'v1.0.0', 'site': 'erp.example.com'}
        (self.root / 'shared/current.json').write_text(json.dumps(state))
        class Commands:
            def __init__(self):
                self.calls = []
            def run(self, args, **kwargs):
                self.calls.append(args)
                return 'id' if 'ps' in args else 'true 0'
        commands = Commands()
        manager.reconcile_ai(self.root, commands=commands)
        migrate = next(i for i, call in enumerate(commands.calls) if 'migrate' in call)
        start = next(i for i, call in enumerate(commands.calls) if 'up' in call and 'ai-api' in call)
        proxy = next(i for i, call in enumerate(commands.calls) if 'up' in call and 'caddy' in call)
        self.assertLess(migrate, start)
        self.assertLess(start, proxy)

    def test_restore_preserves_ai_settings_when_backup_predates_ai(self):
        self.path.write_text(self.base)
        self.path.chmod(0o600)
        manager.disable_ai_after_restore(self.path, {'AI_POSTGRES_PASSWORD': 'current-password', 'AI_WORKER_IMAGE': 'current-image'})
        self.assertIn('AI_POSTGRES_PASSWORD=current-password\n', self.path.read_text())
        self.assertIn('AI_WORKER_IMAGE=current-image\n', self.path.read_text())
        self.assertIn('AI_ENABLED=0\n', self.path.read_text())

    def test_enabled_rejects_reused_database_password(self):
        digest = 'a' * 64
        self.base = self.base.replace('DB_PASSWORD=secret', 'DB_PASSWORD=' + 'd' * 32)
        with self.assertRaisesRegex(ValueError, 'must differ'):
            self.config('AI_ENABLED=1\nAI_API_IMAGE=registry.example/api@sha256:' + digest +
                        '\nAI_WORKER_IMAGE=registry.example/worker@sha256:' + digest +
                        '\nAI_POSTGRES_PASSWORD=' + 'd' * 32 + '\nAI_ERP_SERVICE_KEY=' + 'c' * 32 + '\n')

    def test_enabled_rejects_connector_key_exceeding_client_limit(self):
        digest = 'a' * 64
        with self.assertRaisesRegex(ValueError, 'AI_ERP_SERVICE_KEY'):
            self.config('AI_ENABLED=1\nAI_API_IMAGE=registry.example/api@sha256:' + digest +
                        '\nAI_WORKER_IMAGE=registry.example/worker@sha256:' + digest +
                        '\nAI_POSTGRES_PASSWORD=' + 'b' * 32 + '\nAI_ERP_SERVICE_KEY=' + 'c' * 257 + '\n')

    def test_stop_always_stops_existing_ai_even_when_disabled(self):
        config = self.config('AI_ENABLED=0\nAI_CHAT_ENABLED=true\nAI_CHAT_WEB_DIR=/obsolete/chat\n')
        release = self.root / 'release'
        (release / 'deploy').mkdir(parents=True)
        (release / 'deploy/compose.yml').write_text('services:\n  ai-api:\n')
        class Commands:
            def __init__(self):
                self.calls = []
            def run(self, args, **kwargs):
                self.calls.append(args)
                return ''
        commands = Commands()
        compose = manager.Compose(self.root, release, 'image', commands, config)
        compose.stop()
        self.assertEqual(compose.env['AI_CHAT_WEB_DIR'], '/dev/null')
        self.assertTrue(any('stop' in call and 'ai-api' in call for call in commands.calls))

    def test_release_chat_bundle_replaces_the_configured_fallback(self):
        digest = 'a' * 64
        fallback = self.root / 'fallback'
        (fallback / 'assets').mkdir(parents=True)
        (fallback / 'index.html').write_text('old')
        settings = ('AI_ENABLED=1\nAI_API_IMAGE=registry.example/api@sha256:' + digest +
            '\nAI_WORKER_IMAGE=registry.example/worker@sha256:' + digest +
            '\nAI_POSTGRES_PASSWORD=' + 'b' * 32 + '\nAI_ERP_SERVICE_KEY=' + 'c' * 32 + '\n')
        config = dict(self.config(settings), AI_CHAT_ENABLED='true', AI_CHAT_WEB_DIR=str(fallback))
        release = self.root / 'release'
        (release / 'deploy').mkdir(parents=True)
        (release / 'deploy/compose.yml').write_text('services:\n  caddy:\n')
        compose = manager.Compose(self.root, release, 'image', object(), config)
        self.assertEqual(compose.env['AI_CHAT_WEB_DIR'], str(fallback))
        (release / 'chat-web' / 'assets').mkdir(parents=True)
        (release / 'chat-web' / 'index.html').write_text('new')
        compose = manager.Compose(self.root, release, 'image', object(), config)
        self.assertEqual(compose.env['AI_CHAT_WEB_DIR'], str(release / 'chat-web'))
        disabled = manager.Compose(self.root, release, 'image', object(), dict(config, AI_CHAT_ENABLED='false'))
        self.assertEqual(disabled.env['AI_CHAT_WEB_DIR'], '/dev/null')

    def test_restore_atomic_replace_failure_preserves_live_ai_settings(self):
        from unittest.mock import patch
        backup = self.root / 'backup.env'
        backup.write_text(self.base + 'AI_POSTGRES_PASSWORD=old\n')
        original = self.base + 'AI_POSTGRES_PASSWORD=current\nAI_ENABLED=1\n'
        self.path.write_text(original)
        with patch.object(manager.os, 'replace', side_effect=OSError('injected')):
            with self.assertRaisesRegex(OSError, 'injected'):
                manager.disable_ai_after_restore(self.path, {'AI_POSTGRES_PASSWORD': 'current'}, backup)
        self.assertEqual(self.path.read_text(), original)
        manager.disable_ai_after_restore(self.path, {'AI_POSTGRES_PASSWORD': 'current'}, backup)
        self.assertIn('AI_POSTGRES_PASSWORD=current\n', self.path.read_text())

    def test_reconcile_fails_and_stops_ai_when_api_unhealthy(self):
        import json
        digest = 'a' * 64
        self.config('AI_ENABLED=1\nAI_API_IMAGE=registry.example/api@sha256:' + digest +
                    '\nAI_WORKER_IMAGE=registry.example/worker@sha256:' + digest +
                    '\nAI_POSTGRES_PASSWORD=' + 'b' * 32 + '\nAI_ERP_SERVICE_KEY=' + 'c' * 32 + '\n')
        release = self.root / 'release'
        (release / 'deploy').mkdir(parents=True)
        (release / 'deploy/compose.yml').write_text('services:\n  ai-api:\n')
        (self.root / 'shared/current.json').write_text(json.dumps({
            'release': str(release), 'image': 'ghcr.io/nsivtsev/kanzlei-erp@sha256:' + digest,
            'revision': 'b' * 40, 'tag': 'v1.0.0', 'site': 'erp.example.com'}))
        class Commands:
            def __init__(self):
                self.calls = []
            def run(self, args, **kwargs):
                self.calls.append(args)
                if 'up' in args and '--wait' in args and 'ai-api' in args:
                    raise RuntimeError('unhealthy')
                return ''
        commands = Commands()
        with self.assertRaisesRegex(RuntimeError, 'unhealthy'):
            manager.reconcile_ai(self.root, commands=commands)
        self.assertGreaterEqual(sum('stop' in call and 'ai-api' in call for call in commands.calls), 2)

    def test_restore_removes_indented_backup_ai_entries(self):
        backup = self.root / 'backup.env'
        backup.write_text(self.base + '  AI_ENABLED=1\n\tAI_POSTGRES_PASSWORD=old\n')
        backup.chmod(0o600)
        manager.disable_ai_after_restore(self.path, {'AI_POSTGRES_PASSWORD': 'current'}, backup)
        result = self.path.read_text()
        self.assertNotIn('old', result)
        self.assertEqual(manager.read_config(self.root)['AI_ENABLED'], '0')
        self.assertEqual(manager.read_config(self.root)['AI_POSTGRES_PASSWORD'], 'current')
