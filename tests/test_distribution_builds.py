"""Regression checks for actual setuptools payloads; included with .[dev]."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(importlib.util.find_spec('build') and importlib.util.find_spec('setuptools'),
                     'Distribution rebuild checks require .[dev]')
class DistributionBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        for name in ('.gitignore', 'pyproject.toml', 'setup.py', 'MANIFEST.in', 'VERSION', 'README.md', 'LICENSE'):
            shutil.copy2(str(ROOT / name), str(self.source / name))
        shutil.copytree(ROOT / 'src', self.source / 'src',
                        ignore=shutil.ignore_patterns('__pycache__', '*.egg-info'))

    def build(self, source, output, kind='wheel'):
        result = subprocess.run([sys.executable, '-m', 'build', '--' + kind, '--no-isolation',
                                 '--outdir', str(output), str(source)], cwd=str(self.root),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        artifact, = output.glob('*.whl' if kind == 'wheel' else '*.tar.gz')
        return artifact

    def provenance(self, wheel, package='lite3_dev_server'):
        with zipfile.ZipFile(str(wheel)) as archive:
            return json.loads(archive.read(package + '/_build.json'))

    def unpack_sdist(self):
        archive = self.build(self.source, self.root / 'sdist', 'sdist')
        unpacked = self.root / 'unpacked'
        with tarfile.open(str(archive)) as package:
            package.extractall(str(unpacked))
        source, = unpacked.iterdir()
        return source

    def test_modified_sdist_uses_current_release_and_payload(self):
        source = self.unpack_sdist()
        original = self.provenance(self.build(source, self.root / 'original'))
        (source / 'VERSION').write_text('9.8.7\n', encoding='utf-8')
        module = source / 'src/lite3_dev_server/api.py'
        module.write_bytes(module.read_bytes() + b'\n# downstream change\n')
        patched = self.provenance(self.build(source, self.root / 'patched'))
        self.assertEqual(patched['version'], '9.8.7')
        self.assertNotEqual(patched['build_id'], original['build_id'])
        self.assertIsNone(patched['source_commit'])

    @unittest.skipUnless(shutil.which('git'), 'Git establishes the source revision')
    def test_modified_tools_sdist_cannot_reuse_the_original_commit(self):
        for arguments in (['init'], ['add', '.'],
                          ['-c', 'user.name=SDK test', '-c', 'user.email=sdk@example.invalid',
                           '-c', 'commit.gpgsign=false', 'commit', '-m', 'test: create source fixture']):
            subprocess.run(['git', '-C', str(self.source)] + arguments, check=True, capture_output=True)
        source = self.unpack_sdist()
        original = self.build(source, self.root / 'original')
        original_tools = self.provenance(original, 'lite3_dev_server_tools')
        self.assertIsNotNone(original_tools['source_commit'])
        self.assertIs(original_tools['source_dirty'], False)

        server = source / 'src/server.py'
        server.write_bytes(server.read_bytes() + b'\n# downstream tools change\n')
        rebuilt = self.build(source, self.root / 'patched')
        patched_tools = self.provenance(rebuilt, 'lite3_dev_server_tools')
        self.assertEqual(self.provenance(rebuilt), self.provenance(original))
        self.assertNotEqual(patched_tools['build_id'], original_tools['build_id'])
        self.assertIsNone(patched_tools['source_commit'])
        self.assertIsNone(patched_tools['source_dirty'])


if __name__ == '__main__':
    unittest.main()
