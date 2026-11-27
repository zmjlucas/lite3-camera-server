"""Exercise the dev API through the real HTTP parser without hardware."""
import io
import json
from types import SimpleNamespace
import unittest

from src import server

class Connection:
    def __init__(self, data):
        self.input, self.output = io.BytesIO(data), bytearray()
    def settimeout(self, timeout):
        pass
    def makefile(self, *args, **kwargs):
        return self.input
    def sendall(self, data):
        self.output.extend(data)


class Control:
    def __init__(self):
        self.calls = []
    def status(self):
        return {'mode': 'simulation', 'estop': False}
    def dispatch(self, action, body):
        self.calls.append((action, body))
        return self.status()


class DevHTTPTests(unittest.TestCase):
    def setUp(self):
        self.control = Control()
        self.gateway = SimpleNamespace(read_state=lambda: {'observed': True}, reconnect=lambda: {})
    def request(self, method, path, body=b'{}', headers=()):
        fields = [('Host', 'localhost:8080'), ('Content-Type', 'application/json'),
                  ('Content-Length', str(len(body)))]
        fields.extend(headers)
        raw = '{} {} HTTP/1.1\r\n{}\r\n\r\n'.format(method, path, '\r\n'.join('{}: {}'.format(*x) for x in fields))
        conn = Connection(raw.encode() + body)
        frames = server.LatestFrame('realsense')
        host = SimpleNamespace(frames=frames, control=self.control, arm_gateway=self.gateway)
        server.CameraRequestHandler(conn, ('127.0.0.1', 1), host)
        head, value = bytes(conn.output).split(b'\r\n\r\n', 1)
        return int(head.split()[1]), value
    def test_status_does_not_depend_on_camera(self):
        code, value = self.request('GET', '/api/status')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(value)['mode'], 'simulation')
    def test_trusted_lan_control_and_explicit_arm_read(self):
        self.assertEqual(self.request('POST', '/api/control/acquire', b'{"owner":"test"}')[0], 200)
        self.assertEqual(self.control.calls, [('control/acquire', {'owner':'test'})])
        self.assertEqual(json.loads(self.request('POST', '/api/arm/state')[1]), {'observed': True})
    def test_cross_origin_never_dispatches(self):
        self.assertEqual(self.request('POST', '/api/dog/stand', headers=[('Origin','http://evil')])[0], 403)
        self.assertFalse(self.control.calls)
    def test_rejects_nonobject_nonfinite_duplicate_and_oversized_json(self):
        for body in (b'[]', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1,"x":2}', b'{', b' '*4097):
            with self.subTest(body=body[:30]):
                self.assertIn(self.request('POST','/api/dog/velocity',body)[0], (400,413))
        self.assertFalse(self.control.calls)
    def test_unknown_action_and_extra_arm_read_fields_rejected(self):
        self.assertEqual(self.request('POST', '/api/shell')[0], 404)
        self.assertEqual(self.request('POST', '/api/arm/state', b'{"command":"move"}')[0], 400)
        self.assertFalse(self.control.calls)
    def test_ambiguous_http_framing_rejected(self):
        for headers in ([('Content-Length','2')], [('Transfer-Encoding','chunked')]):
            self.assertEqual(self.request('POST','/api/dog/stop',headers=headers)[0],400)


class DevConfigTests(unittest.TestCase):
    def test_camera_free_simulation_and_explicit_hardware_mode(self):
        options = server.parse_args(['--source','none'])
        self.assertEqual(options.control_mode,'simulation')
        options = server.parse_args(['--source','none','--control-mode','hardware'])
        self.assertEqual(options.control_mode,'hardware')

if __name__ == '__main__':
    unittest.main()
