"""Observe startup identity over HTTP while deployed files change."""
from contextlib import contextmanager, redirect_stdout
import io
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener
from src.launcher import SERVER_BUNDLE
from src import launcher
from src.lite3_dev_server._build_info import BuildInfo, source_files, with_files, write_manifest
from src.remote.manage_service import ServiceManager


class RunningBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for source, destination in SERVER_BUNDLE:
            path = self.root / Path(destination).relative_to('lite3-dev-server') / source.name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source.read_bytes())

    @contextmanager
    def running(self):
        process = subprocess.Popen([
            sys.executable, '-E', '-S', '-B', '-u', str(self.root / 'server.py'),
            '--source', 'none', '--env', 'dev', '--simulate', '--bind', '127.0.0.1', '--port', '0',
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(self.root))
        lines = queue.Queue()
        threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
        try:
            origin = lines.get(timeout=10).strip()
            if not origin.startswith('http://127.0.0.1:'):
                self.fail('Server did not start: ' + process.stderr.read())
            yield origin
        finally:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)

    def headers(self, origin, path='/api/status'):
        try:
            response = build_opener(ProxyHandler({})).open(origin + path, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            response.read()
            return response.headers

    def test_replacing_files_keeps_running_identity_until_restart(self):
        manifest = self.root / '_build.json'
        files = list(source_files(self.root))
        first = with_files(BuildInfo('0.2.0', 'a' * 40, False, 'unknown'), files)
        second = with_files(BuildInfo('0.2.1', 'b' * 40, False, 'unknown'), files)
        write_manifest(manifest, first)
        with self.running() as origin:
            for path in ('/api/status', '/healthz', '/missing'):
                headers = self.headers(origin, path)
                self.assertEqual(headers.get_all('X-Lite3-Dev-Protocol'), ['1'])
                self.assertEqual(headers.get_all('X-Lite3-Build'), [first.build_id])
                self.assertEqual(headers['X-Lite3-Release'], first.version)
                self.assertEqual(headers['X-Lite3-Source-Commit'], first.source_commit)
                self.assertEqual(headers['X-Lite3-Source-Dirty'], 'false')
            self.assertEqual(ServiceManager(port=urlsplit(origin).port).http_identity()[1], first.build_id)
            write_manifest(manifest, second)
            self.assertEqual(self.headers(origin)['X-Lite3-Build'], first.build_id)
        with self.running() as origin:
            headers = self.headers(origin)
            self.assertEqual(headers['X-Lite3-Dev-Protocol'], '1')
            self.assertEqual(headers['X-Lite3-Build'], second.build_id)
            self.assertEqual(headers['X-Lite3-Release'], second.version)
            self.assertEqual(headers['X-Lite3-Source-Commit'], second.source_commit)

    def test_partial_update_reports_unknown_after_restart(self):
        from src.lite3_dev_server._build_info import capture_build, source_files
        info = capture_build(self.root, source_files(self.root))
        write_manifest(self.root / '_build.json', info)
        with self.running() as origin:
            self.assertEqual(self.headers(origin)['X-Lite3-Build'], info.build_id)
            source = self.root / 'server.py'
            source.write_bytes(source.read_bytes() + b'\n# partial deployment\n')
            self.assertEqual(self.headers(origin)['X-Lite3-Build'], info.build_id)
        with self.running() as origin:
            self.assertEqual(self.headers(origin)['X-Lite3-Build'], 'unknown')


class DeploymentBuildTests(unittest.TestCase):

    def test_manifest_and_uploads_use_snapshot_taken_before_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / 'src' / 'server.py'
            source.parent.mkdir()
            source.write_bytes(b'original code')
            version = root / 'VERSION'
            version.write_text('0.2.0\n', encoding='utf-8')
            uploads = {}

            def transport(command):
                if command[0] == 'scp':
                    path = Path(command[-2])
                    uploads[path.name] = path.read_bytes()
                else:
                    source.write_bytes(b'changed during SSH')
                    version.write_text('0.3.0\n', encoding='utf-8')
                return 0

            args = launcher.parser().parse_args(['deploy', 'robot', '--backend', 'none', '--direct'])
            with patch.object(launcher, 'SERVER', source), \
                    patch.object(launcher, 'SERVER_BUNDLE', ((source, 'lite3-dev-server/'),)), \
                    patch.object(launcher.subprocess, 'call', side_effect=transport), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(launcher.deploy(args, None), 0)
            self.assertEqual(uploads['server.py'], b'original code')
            self.assertIn('_build.json', uploads)
            manifest = json.loads(uploads['_build.json'])
            self.assertEqual(manifest['version'], '0.2.0')
            self.assertIsNone(manifest['source_commit'])
            self.assertIsNone(manifest['source_dirty'])
            self.assertRegex(manifest['build_id'], '^sha256:[0-9a-f]{64}$')
            self.assertIn('Deployed build: ' + manifest['build_id'], output.getvalue())


if __name__ == '__main__':
    unittest.main()
