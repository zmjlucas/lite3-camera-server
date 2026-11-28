"""Exercise the dev API through the real HTTP parser without hardware."""
import json
from types import SimpleNamespace
import unittest

from src import arm_gateway, control, server
from src.application import ControlApplication
from src.camera import LatestFrame
from src.http_server import CameraRequestHandler
from src.remote.arm_worker import observed_state
from fixtures.http_connection import MemoryConnection


class Control(control.ControlService):
    def __init__(self):
        super().__init__()
        self.calls = []

    def dispatch(self, action, body):
        self.calls.append((action, body))
        return self.status()


class DevHTTPTests(unittest.TestCase):
    def setUp(self):
        self.control = Control()
        self.addCleanup(self.control.close)
        self.measured = observed_state({'b': 1., 's': 2., 'e': 3., 'w': 4.})
        self.gateway = SimpleNamespace(read_state=lambda: dict(self.measured))
    def request(self, method, path, body=b'{}', headers=()):
        fields = [('Host', 'localhost:8080'), ('Content-Type', 'application/json'),
                  ('Content-Length', str(len(body)))]
        fields.extend(headers)
        raw = '{} {} HTTP/1.1\r\n{}\r\n\r\n'.format(method, path, '\r\n'.join('{}: {}'.format(*x) for x in fields))
        conn = MemoryConnection(raw.encode() + body)
        frames = LatestFrame('realsense')
        host = SimpleNamespace(frames=frames, application=ControlApplication(self.control, self.gateway))
        CameraRequestHandler(conn, ('127.0.0.1', 1), host)
        head, value = bytes(conn.output).split(b'\r\n\r\n', 1)
        return int(head.split()[1]), value
    def test_status_does_not_depend_on_camera(self):
        code, value = self.request('GET', '/api/status')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(value)['mode'], 'simulation')
    def test_trusted_lan_control_and_explicit_arm_read(self):
        self.assertEqual(self.request('POST', '/api/control/acquire', b'{"owner":"test"}')[0], 200)
        self.assertEqual(self.control.calls, [('control/acquire', {'owner':'test'})])
        self.assertEqual(json.loads(self.request('POST', '/api/arm/state')[1]), self.measured)
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
    def test_domain_errors_keep_their_http_status_and_semantic_code(self):
        self.control = control.ControlService()
        self.addCleanup(self.control.close)
        for action, body, status, code in (
                ('arm/state', b'{"extra":1}', 400, 'invalid_request'),
                ('arm/operation', b'{"id":"unknown"}', 404, 'not_found'),
                ('control/heartbeat', b'{"lease":"expired"}', 409, 'conflict')):
            with self.subTest(action=action):
                received, value = self.request('POST', '/api/' + action, body)
                self.assertEqual(received, status)
                self.assertEqual(json.loads(value)['code'], code)
        self.control.dispatch('control/estop', {})
        status, body = self.request('POST', '/api/control/acquire', b'{"owner":"operator"}')
        self.assertEqual((status, json.loads(body)['code']), (423, 'locked'))
        self.control.close()
        status, body = self.request('POST', '/api/control/acquire', b'{"owner":"operator"}')
        self.assertEqual((status, json.loads(body)['code']), (503, 'unavailable'))

    def test_ambiguous_http_framing_rejected(self):
        for headers in ([('Content-Length','2')], [('Transfer-Encoding','chunked')]):
            self.assertEqual(self.request('POST','/api/dog/stop',headers=headers)[0],400)


    def test_status_and_control_responses_match_sdk_dictionary_shapes(self):
        from src.lite3_dev_server import AcquireResponse, ArmStatus, ControlStatus, DogStatus

        self.control = control.ControlService(mode='disabled')
        self.addCleanup(self.control.close)
        _, body = self.request('GET', '/api/status')
        self.assertEqual(json.loads(body).keys(), ControlStatus.__annotations__.keys())

        self.control = control.ControlService()
        self.addCleanup(self.control.close)
        code, body = self.request('POST', '/api/control/acquire', b'{"owner":"contract"}')
        reply = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(reply.keys(), AcquireResponse.__annotations__.keys())
        self.assertIsInstance(reply['lease'], str)
        self.assertEqual(reply['status'].keys(), ControlStatus.__annotations__.keys())
        self.assertEqual(reply['status']['dog'].keys(), DogStatus.__annotations__.keys())
        self.assertEqual(reply['status']['arm'].keys(), {'motion_enabled', 'reason', 'position_deg', 'simulated'})
        self.assertLessEqual(reply['status']['arm'].keys(), ArmStatus.__annotations__.keys())

    def test_arm_state_and_reconnect_keep_distinct_gateway_shapes(self):
        from src.lite3_dev_server import ArmGatewayReconnect, ObservedArmState, UnobservedArmState

        self.control = control.ControlService()
        self.addCleanup(self.control.close)
        self.gateway = None
        for action in ('state', 'reconnect'):
            with self.subTest(action=action):
                code, body = self.request('POST', '/api/arm/' + action)
                summary = json.loads(body)
                self.assertEqual(code, 200)
                self.assertEqual(summary.keys(), {'motion_enabled', 'reason', 'position_deg', 'simulated', 'observed'})
                self.assertLessEqual(summary.keys(), UnobservedArmState.__annotations__.keys())
                self.assertIs(summary['observed'], False)
                self.assertEqual(set(summary['position_deg']), {'b', 's', 'e', 'w', 'h'})

        angles = {'b': 10.0, 's': -5.0, 'e': 2.0, 'w': 0.0}
        gateway = arm_gateway.ArmGateway()
        self.addCleanup(gateway.close)
        self.gateway = SimpleNamespace(read_state=lambda: observed_state(angles), reconnect=gateway.reconnect)
        code, body = self.request('POST', '/api/arm/state')
        measured = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(measured.keys(), {'position_deg', 'gripper_position_deg', 'observed', 'motion_enabled', 'reason'})
        self.assertLessEqual(measured.keys(), ObservedArmState.__annotations__.keys())
        self.assertIs(measured['observed'], True)
        self.assertEqual(measured['position_deg'], angles)
        self.assertIsNone(measured['gripper_position_deg'])

        code, body = self.request('POST', '/api/arm/reconnect')
        reconnected = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(reconnected.keys(), ArmGatewayReconnect.__annotations__.keys())
        self.assertIs(reconnected['connected'], False)
        self.assertIs(reconnected['reconnect_required'], False)

    def test_arm_operation_and_stop_routes_reach_control(self):
        self.assertEqual(self.request('POST', '/api/arm/operation', b'{"id":"op"}')[0], 200)
        self.assertEqual(self.control.calls[-1], ('arm/operation', {'id': 'op'}))
        self.assertEqual(self.request('POST', '/api/arm/stop')[0], 200)
        self.assertEqual(self.control.calls[-1], ('arm/stop', {}))

    def test_arm_read_rechecks_estop_after_serial_io(self):
        self.control = control.ControlService(mode='hardware', dog_enabled=False)
        self.addCleanup(self.control.close)
        def read_then_estop():
            self.control.dispatch('control/estop', {})
            return dict(observed_state({'b': 0., 's': 0., 'e': 0., 'w': 0.}),
                        motion_enabled=True, reason=None)
        self.gateway = SimpleNamespace(read_state=read_then_estop)
        code, body = self.request('POST', '/api/arm/state')
        state = json.loads(body)
        self.assertEqual(code, 200)
        self.assertFalse(state['motion_enabled'])
        self.assertIn('emergency stop', state['reason'].lower())

    def test_arm_profile_is_local_hardware_only(self):
        args = server.parse_args(['--source', 'none', '--control-mode', 'hardware',
                                  '--arm-motion-profile', '/home/ysc/arm/session.json'])
        self.assertEqual(args.arm_motion_profile, '/home/ysc/arm/session.json')
        for options in ([], ['--control-mode', 'readonly'],
                        ['--control-mode', 'hardware', '--arm-host', 'robot']):
            with self.subTest(options=options), self.assertRaises(SystemExit):
                server.parse_args(['--source', 'none', '--arm-motion-profile', '/tmp/profile'] + options)


if __name__ == '__main__':
    unittest.main()
