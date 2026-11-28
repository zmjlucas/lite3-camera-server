"""Exercise the dev API through the real HTTP parser without hardware."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src import control
from src.application import ControlApplication
from src.camera import LatestFrame
from src.http_server import CameraRequestHandler
from src.lite3_dev_server._build_info import UNKNOWN_BUILD
from fixtures.http_connection import MemoryConnection
from tests.test_control import Clock, prepare_dog


class DevHTTPTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.control = control.ControlService(clock=self.clock, environment='dev', simulate=True)
        self.addCleanup(self.control.close)

    def post(self, action, body=b'{}', headers=()):
        fields = [('Host', 'localhost:8080'), ('Content-Type', 'application/json'),
                  ('Content-Length', str(len(body))), *headers]
        header_lines = '\r\n'.join('{}: {}'.format(*field) for field in fields)
        raw = 'POST /api/{} HTTP/1.1\r\n{}\r\n\r\n'.format(action, header_lines)
        conn = MemoryConnection(raw.encode() + body)
        host = SimpleNamespace(frames=LatestFrame('none'), application=ControlApplication(self.control),
                               build_info=UNKNOWN_BUILD)
        CameraRequestHandler(conn, ('127.0.0.1', 1), host)

        head, payload = bytes(conn.output).split(b'\r\n\r\n', 1)
        return int(head.split()[1]), json.loads(payload)

    def test_dog_action_lookup_is_read_only_including_response_headers_and_errors(self):
        lease = self.control.dispatch('control/acquire', {'owner': 'actions'})['lease']
        prepare_dog(self.control, self.clock, lease)
        action = self.control.dispatch('dog/action', dict(lease=lease, seq=2, name='forward',
                                                         speed=.1, duration=1))['dog']['action']
        self.control._tick()
        self.clock.advance(2)
        before = self.control.snapshot()
        body = json.dumps({'id': action['id']}).encode()
        self.assertEqual(self.post('dog/action/status', body), (200, dict(action, remaining_s=0.0)))
        status, response = self.post('dog/action/status', b'{"id":"missing"}')
        self.assertEqual((status, response['code']), (404, 'not_found'))
        self.assertEqual(self.control.snapshot(), before)

        self.control._tick()
        expected = dict(action, phase='cancelled', reason='expired', remaining_s=0.0)
        self.assertEqual(self.post('dog/action/status', body), (200, expected))

    def test_rejects_nonobject_nonfinite_duplicate_and_oversized_json(self):
        with patch.object(self.control, 'dispatch') as dispatch:
            for body, expected in ((b'[]', 400), (b'{"x":NaN}', 400),
                                   (b'{"x":1,"x":2}', 400), (b' ' * 4097, 413)):
                with self.subTest(body=body[:30]):
                    self.assertEqual(self.post('dog/velocity', body)[0], expected)
            dispatch.assert_not_called()

    def test_domain_errors_keep_their_http_status_and_semantic_code(self):
        for action, body, status, code in (
                ('arm/state', b'{"extra":1}', 400, 'invalid_request'),
                ('arm/operation', b'{"id":"unknown"}', 404, 'not_found'),
                ('control/heartbeat', b'{"lease":"expired"}', 409, 'conflict')):
            with self.subTest(action=action):
                received, value = self.post(action, body)
                self.assertEqual((received, value['code']), (status, code))
        self.control.dispatch('control/estop', {})
        status, body = self.post('control/acquire', b'{"owner":"operator"}')
        self.assertEqual((status, body['code']), (423, 'locked'))
        self.control.close()
        status, body = self.post('control/acquire', b'{"owner":"operator"}')
        self.assertEqual((status, body['code']), (503, 'unavailable'))

    def test_ambiguous_http_framing_rejected(self):
        with patch.object(self.control, 'dispatch') as dispatch:
            for header in (('Content-Length', '2'), ('Transfer-Encoding', 'chunked')):
                status, body = self.post('dog/stop', headers=[header])
                self.assertEqual((status, body['code']), (400, 'invalid_request'))
            dispatch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
