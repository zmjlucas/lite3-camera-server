"""Artifact provenance survives copying and identifies changed source bytes."""
import importlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


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


if __name__ == '__main__':
    unittest.main()
