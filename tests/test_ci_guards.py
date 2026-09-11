"""Tests for the CI guards that keep secrets and runtime DBs out of git."""
import importlib.util
import os
import subprocess
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_guard():
    path = os.path.join(_REPO_ROOT, 'scripts', 'check_tracked_artifacts.py')
    spec = importlib.util.spec_from_file_location('check_tracked_artifacts', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TrackedArtifactGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guard = _load_guard()

    def test_forbidden_paths_are_detected(self):
        forbidden = [
            'instance/servers.db',
            'instance/backups/backup_20251206_210932.db',
            'runtime/servers.sqlite3',
            'deploy/privkey.pem',
            'certs/server.key',
            '.env',
            'config/.env.production',
            'id_rsa',
            'foo.bak',
            'dashboard.html.backup',
            'backup.dump',
            'keystore.jks',
            'thing.eveenc',
        ]
        for path in forbidden:
            self.assertTrue(self.guard.is_forbidden(path), path)

    def test_allowed_paths_are_not_flagged(self):
        allowed = [
            '.env.docker.example',
            'app.py',
            'panel/services/backup.py',
            'docs/security/BACKUP_POLICY.md',
            'tests/test_backup_policy.py',
            'alembic/versions/b2c3d4e5f6a7_wallet_ledger.py',
            'bnqo/Cargo.lock',
            'static/css/app.css',
        ]
        for path in allowed:
            self.assertFalse(self.guard.is_forbidden(path), path)

    def test_repository_tree_passes_the_guard(self):
        guard_path = os.path.join(_REPO_ROOT, 'scripts', 'check_tracked_artifacts.py')
        result = subprocess.run(
            [sys.executable, guard_path], cwd=_REPO_ROOT,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_main_reports_offenders_and_fails(self):
        rc = self.guard.main(['--path', 'instance/servers.db', '--path', 'app.py'])
        self.assertEqual(rc, 1)


if __name__ == '__main__':
    unittest.main()
