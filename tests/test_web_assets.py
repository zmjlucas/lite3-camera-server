"""Serve the browser API client through an explicit, same-origin asset route."""
from types import SimpleNamespace
import unittest

from src.camera import LatestFrame
from src.http_server import CameraRequestHandler
from fixtures.http_connection import MemoryConnection


class WebAssetTests(unittest.TestCase):
    def request(self, path, origin=None):
        headers = 'Host: localhost:8080\r\n'
        if origin is not None:
            headers += 'Origin: ' + origin + '\r\n'
        connection = MemoryConnection(('GET ' + path + ' HTTP/1.1\r\n' + headers + '\r\n').encode())
        CameraRequestHandler(connection, ('127.0.0.1', 1), SimpleNamespace(frames=LatestFrame('none')))
        headers, body = bytes(connection.output).split(b'\r\n\r\n', 1)
        return int(headers.split()[1]), headers, body

    def test_control_page_loads_explicit_javascript_asset(self):
        code, headers, body = self.request('/control')
        self.assertEqual(code, 200)
        self.assertIn(b'src="/static/dev_client.js"', body)
        self.assertIn(b"script-src 'self' 'unsafe-inline'", headers)
        code, headers, body = self.request('/static/dev_client.js')
        self.assertEqual(code, 200)
        self.assertIn(b'application/javascript', headers)
        self.assertIn(b'class Lite3DevClient', body)

    def test_asset_path_is_allowlisted_and_same_origin(self):
        for path in ('/static/../control.py', '/static/%2e%2e/control.py', '/static/unknown.js'):
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request('/static/dev_client.js', 'http://foreign.example')[0], 403)
        self.assertEqual(self.request('/static/dev_client.js', 'http://localhost:8080')[0], 200)
