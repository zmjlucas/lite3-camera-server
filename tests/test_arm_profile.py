"""Profile tooling checks local files and metadata without serial access."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
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

    def test_template_keeps_operator_references_empty_and_refuses_overwrite(self):
        self.run_tool('template', 0)
        record = json.loads(self.path.read_text())
        self.assertEqual((record['machine_id'], record['boot_id'], record['device']), ('host', 'boot', '/dev/null'))
        self.assertTrue(record['nonce'])
        self.assertEqual(record['targets_deg'], dict(b=None, s=None, e=None, w=None, h=None))
        self.assertEqual(record['observed_deg'], dict(b=None, s=None, e=None, w=None))
        self.assertIsNone(record['gripper_reference'])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.run_tool('check', 1)
        original = self.path.read_bytes()
        self.run_tool('template', 1)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.directory.glob('.r5-used-*')), [])

    def test_check_reuses_server_validation_without_claim_or_device_open(self):
        self.save()
        original = self.path.read_bytes()
        real_open = os.open
        opened = []
        def tracked_open(path, *args, **kwargs):
            opened.append(os.fspath(path))
            if os.fspath(path).startswith('/dev/'):
                raise AssertionError('Profile tooling opened a device')
            return real_open(path, *args, **kwargs)
        with mock.patch('os.open', side_effect=tracked_open):
            for _ in range(2):
                output, _ = self.run_tool('check', 0)
                result = json.loads(output)
                self.assertEqual(result['commissioning']['state'], 'pending_preflight')
                self.assertFalse(result['commissioning']['nonce_consumed'])
                self.assertEqual(result['device']['path'], '/dev/null')
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

    def test_check_rejects_mismatch_offsets_permissions_and_symlinks(self):
        for field, value in (
                ('machine_id', 'different-host'), ('boot_id', 'different-boot'),
                ('device', '/dev/zero'), ('gripper_reference', ' '),
                ('targets_deg', dict(b=10, s=-4, e=8, w=6, h=True)),
                ('observed_deg', dict(b=10.1, s=-4.2, e=20, w=6.05))):
            with self.subTest(field=field):
                old = self.record[field]
                self.record[field] = value
                self.save()
                self.run_tool('check', 1)
                self.record[field] = old
        self.save()
        self.path.chmod(0o666)
        self.run_tool('check', 1)
        self.save()
        target = self.directory / 'target.json'
        self.path.rename(target)
        self.path.symlink_to(target)
        self.run_tool('check', 1)
        self.assertEqual(list(self.directory.glob('.r5-used-*')), [])

    def test_check_rejects_missing_noncharacter_or_inaccessible_device(self):
        self.save()
        with mock.patch.object(self.tool, '_device_metadata', side_effect=FileNotFoundError('Missing device')):
            self.run_tool('check', 1)
        with mock.patch.object(self.tool.stat, 'S_ISCHR', return_value=False):
            self.run_tool('check', 1)
        with mock.patch.object(self.tool.os, 'access', return_value=False):
            self.run_tool('check', 1)
        self.assertEqual(list(self.directory.glob('.r5-used-*')), [])

    def test_unsafe_directory_cannot_generate_or_validate_profile(self):
        self.directory.chmod(0o777)
        self.run_tool('template', 1)
        self.assertFalse(self.path.exists())
        self.save()
        self.run_tool('check', 1)

    def test_device_given_as_profile_is_rejected_before_open(self):
        self.path = Path('/dev/null')
        real_open = os.open
        def reject_device_open(path, *args, **kwargs):
            if os.fspath(path) in ('null', '/dev/null'):
                raise AssertionError('Checker opened the device supplied as a profile')
            return real_open(path, *args, **kwargs)
        with mock.patch('os.open', side_effect=reject_device_open):
            self.run_tool('check', 1)
