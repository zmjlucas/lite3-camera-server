"""The launcher works outside the checkout."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SRC = Path(__file__).resolve().parents[1] / 'src'


class LauncherEntrypointTests(unittest.TestCase):
    def test_deployment_dry_run_outside_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, '-B', str(SRC / 'launcher.py'), 'deploy',
                 'robot', '--backend', 'none', '--direct', '--dry-run'],
                cwd=directory, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('server.py', result.stdout)


if __name__ == '__main__':
    unittest.main()
