import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class LocalAISetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'scripts').mkdir()
        (self.root / 'services/kanzlei_ai').mkdir(parents=True)
        (self.root / 'bin').mkdir()
        shutil.copy(ROOT / 'scripts/setup-local.sh', self.root / 'scripts/setup-local.sh')
        shutil.copy(ROOT / 'services/kanzlei_ai/.env.example', self.root / 'services/kanzlei_ai/.env.example')
        (self.root / 'scripts/setup-minio.sh').write_text('#!/bin/sh\nexit 0\n')
        (self.root / '.env').write_text('EXISTING=1\n')
        docker = self.root / 'bin/docker'
        docker.write_text('#!/bin/sh\nexit 1\n')
        docker.chmod(0o755)

    def run_setup_until_config(self):
        env = dict(os.environ, PATH=str(self.root / 'bin') + ':' + os.environ['PATH'])
        result = subprocess.run(['bash', str(self.root / 'scripts/setup-local.sh')], env=env,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)

    def test_existing_ai_credentials_are_untouched(self):
        path = self.root / 'services/kanzlei_ai/.env'
        original = "POSTGRES_PASSWORD='p@$word'\nAI_DATABASE_PASSWORD='p@$word'\nCUSTOM=value\n"
        path.write_text(original)
        self.run_setup_until_config()
        self.assertEqual(path.read_text(), original)

    def test_fresh_setup_generates_matching_private_credentials(self):
        self.run_setup_until_config()
        path = self.root / 'services/kanzlei_ai/.env'
        values = dict(line.split('=', 1) for line in path.read_text().splitlines()
                      if line and not line.startswith('#') and '=' in line)
        self.assertEqual(values['POSTGRES_PASSWORD'], values['AI_DATABASE_PASSWORD'])
        self.assertGreaterEqual(len(values['POSTGRES_PASSWORD']), 32)
        self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_existing_ai_consumers_stop_before_migration(self):
        log = self.root / 'calls'
        docker = self.root / 'bin/docker'
        docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_CALL_LOG"\n'
                          'case " $* " in *" kanzlei_ai.coordinator migrate "*) exit 1;; esac\nexit 0\n')
        docker.chmod(0o755)
        env = dict(os.environ, PATH=str(self.root / 'bin') + ':' + os.environ['PATH'], TEST_CALL_LOG=str(log))
        result = subprocess.run(['bash', str(self.root / 'scripts/setup-local.sh')], env=env,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        calls = log.read_text().splitlines()
        migration = next(i for i, call in enumerate(calls) if 'kanzlei_ai.coordinator migrate' in call)
        stop = next(i for i, call in enumerate(calls) if 'stop ai-api ai-worker ai-coordinator' in call)
        self.assertLess(stop, migration)
