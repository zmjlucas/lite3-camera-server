"""Artifact provenance survives copying and identifies changed source bytes."""
import importlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


class BuildInfoTests(unittest.TestCase):
    def setUp(self):
        self.builds = importlib.import_module('src.lite3_dev_server._build_info')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'VERSION').write_text('0.2.0\n', encoding='utf-8')

    def git(self, *arguments):
        return subprocess.check_output(['git', '-C', str(self.root), *arguments],
                                       stderr=subprocess.DEVNULL, text=True).strip()

    def commit_source(self):
        if not shutil.which('git'):
            self.skipTest('Git is needed for source provenance acceptance')
        self.git('init')
        self.git('add', 'VERSION')
        self.git('-c', 'user.name=SDK test', '-c', 'user.email=sdk@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '-m', 'test: create source fixture')
        return self.git('rev-parse', 'HEAD')

    def test_clean_commit_and_dirty_content_are_distinguishable(self):
        commit = self.commit_source()
        first = self.builds.capture_build(self.root, [('module.py', b'first')])
        self.assertEqual(first.version, '0.2.0')
        self.assertEqual(first.source_commit, commit)
        self.assertIs(first.source_dirty, False)
        (self.root / 'VERSION').write_text('0.2.1\n', encoding='utf-8')
        second = self.builds.capture_build(self.root, [('module.py', b'first')])
        third = self.builds.capture_build(self.root, [('module.py', b'second')])
        self.assertIs(second.source_dirty, True)
        self.assertEqual(second.source_commit, commit)
        self.assertNotEqual(first.build_id, second.build_id)
        self.assertNotEqual(second.build_id, third.build_id)

    def test_copy_inside_unrelated_git_repository_has_unknown_origin(self):
        self.commit_source()
        copied = self.root / 'copied'
        copied.mkdir()
        shutil.copyfile(str(self.root / 'VERSION'), str(copied / 'VERSION'))
        info = self.builds.capture_build(copied, [('module.py', b'code')])
        self.assertEqual(info.version, '0.2.0')
        self.assertIsNone(info.source_commit)
        self.assertIsNone(info.source_dirty)

    def test_missing_git_keeps_release_and_explicit_unknown_source(self):
        (self.root / '.git').mkdir()
        with patch.object(self.builds.subprocess, 'run', side_effect=FileNotFoundError('git')):
            info = self.builds.capture_build(self.root, [('module.py', b'code')])
        self.assertEqual(info.version, '0.2.0')
        self.assertIsNone(info.source_commit)
        self.assertIsNone(info.source_dirty)

    def test_manifest_roundtrip_preserves_build_outside_git(self):
        self.commit_source()
        (self.root / 'module.py').write_bytes(b'code')
        info = self.builds.capture_build(self.root, [('module.py', b'code')])
        manifest = self.root / '_build.json'
        self.builds.write_manifest(manifest, info)
        shutil.rmtree(str(self.root / '.git'))
        self.assertEqual(self.builds.read_manifest(manifest), info)

    def test_modified_payload_invalidates_embedded_identity(self):
        source = self.root / 'module.py'
        source.write_bytes(b'original')
        info = self.builds.capture_build(self.root, [('module.py', b'original')])
        manifest = self.root / '_build.json'
        self.builds.write_manifest(manifest, info)
        self.assertEqual(self.builds.read_manifest(manifest), info)
        source.write_bytes(b'changed after packaging')
        self.assertIsNone(self.builds.read_manifest(manifest))

    def test_malformed_manifest_is_ignored(self):
        manifest = self.root / '_build.json'
        (self.root / 'module.py').write_bytes(b'code')
        self.commit_source()
        valid = dict(self.builds.capture_build(self.root, [('module.py', b'code')])._asdict(),
                     files=['module.py'])
        self.builds.write_manifest(manifest, self.builds.capture_build(self.root, [('module.py', b'code')]))
        self.assertIsNotNone(self.builds.read_manifest(manifest))
        for data in ([], {}, dict(valid, version='0.2.0\r\nInjected: yes'),
                     dict(valid, source_commit='bad'), dict(valid, source_dirty=1),
                     dict(valid, build_id='oops'), dict(valid, source_commit=None),
                     dict(valid, source_dirty=None)):
            with self.subTest(data=data):
                manifest.write_text(json.dumps(data), encoding='utf-8')
                self.assertIsNone(self.builds.read_manifest(manifest))
        manifest.write_bytes(b'\xff')
        self.assertIsNone(self.builds.read_manifest(manifest))


if __name__ == '__main__':
    unittest.main()
