"""Profile tooling checks local files and metadata without serial access."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from src.arm_motion import _Profile
from scripts import arm_profile


@unittest.skipUnless(os.name == 'posix', 'Local commissioning uses POSIX file metadata')
class ProfileToolTests(unittest.TestCase):
    def setUp(self):
        self.tool = arm_profile
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / 'profile.json'
        identity = mock.patch.object(self.tool, '_identity', return_value=('host', 'boot'))
        identity.start()
        self.addCleanup(identity.stop)
        self.record = {'version': 1, 'machine_id': 'host', 'boot_id': 'boot',
                       'device': '/dev/null', 'nonce': 'offline-profile',
                       'targets_deg': dict(b=10, s=-4, e=8, w=6, h=-16),
                       'observed_deg': dict(b=10.1, s=-4.2, e=11.69, w=6.05),
                       'gripper_reference': 'Independent offline fixture reference'}

    def save(self):
        self.path.write_text(json.dumps(self.record))
        self.path.chmod(0o600)

    def run_tool(self, command, expected):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                result = self.tool.main([command, str(self.path), '--device', '/dev/null'])
        self.assertEqual(result, expected, errors.getvalue())
        return output.getvalue(), errors.getvalue()

    def test_check_reuses_server_validation_without_claim_or_device_open(self):
        self.save()
        original = self.path.read_bytes()
        real_open = os.open
        def tracked_open(path, *args, **kwargs):
            if os.fspath(path).startswith('/dev/'):
                raise AssertionError('Profile tooling opened a device')
            return real_open(path, *args, **kwargs)
        with mock.patch('os.open', side_effect=tracked_open):
            output, _ = self.run_tool('check', 0)
            result = json.loads(output)
            self.assertEqual(result['commissioning']['state'], 'pending_preflight')
            self.assertFalse(result['commissioning']['nonce_consumed'])
            self.assertTrue(result['device']['readable'])
            self.assertTrue(result['device']['writable'])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.directory.glob('.r5-used-*')), [])
        profile = _Profile(str(self.path), '/dev/null', lambda: ('host', 'boot'))
        try:
            profile.claim()
        finally:
            profile.close()
        self.run_tool('check', 1)
        self.assertEqual(len(list(self.directory.glob('.r5-used-*'))), 1)
