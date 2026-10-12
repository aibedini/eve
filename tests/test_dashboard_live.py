"""Browser-independent JS regressions, run by the existing Python CI suite."""
from pathlib import Path
import shutil
import subprocess
import unittest


class DashboardLiveTests(unittest.TestCase):
    def test_dashboard_live_regressions(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node.js is required for dashboard JS regressions')
        result = subprocess.run(
            [node, '--test', str(Path(__file__).with_name('dashboard_live.test.cjs'))],
            capture_output=True, text=True, encoding='utf-8', timeout=25,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
