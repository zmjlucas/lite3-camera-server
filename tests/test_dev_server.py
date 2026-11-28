"""Exercise the dev API through the real HTTP parser without hardware."""
import json
import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src import arm_gateway, control, server
from src.remote.arm_worker import observed_state
from fixtures.http_connection import MemoryConnection


class Control:
    def __init__(self):
        self.calls = []
    def status(self):
        return {'mode': 'hardware', 'environment': 'dev', 'estop': False}
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
        conn = MemoryConnection(raw.encode() + body)
        frames = server.LatestFrame('realsense')
        host = SimpleNamespace(frames=frames, control=self.control, arm_gateway=self.gateway)
        server.CameraRequestHandler(conn, ('127.0.0.1', 1), host)
        head, value = bytes(conn.output).split(b'\r\n\r\n', 1)
        self.response_head = head
        return int(head.split()[1]), value
    def test_status_does_not_depend_on_camera(self):
        code, value = self.request('GET', '/api/status')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(value)['mode'], 'hardware')

    def test_dog_action_dispatches_through_http_and_checks_readiness(self):
        from test_control import Clock
        clock = Clock()
        self.control = control.ControlService(clock=clock, environment='dev', simulate=True)
        self.addCleanup(self.control.close)
        _, body = self.request('POST', '/api/control/acquire', b'{"owner":"actions"}')
        lease = json.loads(body)['lease']
        action = dict(lease=lease, seq=2, name='turn_left', speed=.2, duration=1)
        self.assertEqual(self.request('POST', '/api/dog/action', json.dumps(action).encode())[0], 409)
        self.request('POST', '/api/dog/stand', json.dumps(dict(lease=lease, seq=1)).encode())
        self.control._tick()
        for elapsed in (1.5, 1.5, 1.5, .5, .21, .21, .21):
            clock.advance(elapsed)
            self.request('POST', '/api/control/heartbeat', json.dumps(dict(lease=lease)).encode())
            self.control._tick()
        code, body = self.request('POST', '/api/dog/action', json.dumps(action).encode())
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['dog']['velocity']['yaw'], -.2)
        self.assertEqual(json.loads(body)['dog']['action']['phase'], 'running')
        self.control._tick()
        clock.advance(1)
        _, body = self.request('GET', '/api/status')
        self.assertEqual(json.loads(body)['dog']['action']['phase'], 'completed')
        self.assertFalse(json.loads(body)['lease_active'])

    def test_dog_state_port_rejects_invalid_values(self):
        for port in ('0', '65536', '-1'):
            with self.subTest(port=port), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    server.parse_args(['--source', 'none', '--dog-state-port', port])

    def test_default_server_constructs_real_dist_control(self):
        options = server.parse_args(['--source', 'none'])
        service, gateway = server.development_services(options)
        try:
            self.assertEqual(service.status()['mode'], 'hardware')
            self.assertEqual(service.status()['environment'], 'dist')
            self.assertIsInstance(gateway, arm_gateway.ArmGateway)
            self.assertFalse(service.status()['arm']['simulated'])
        finally:
            service.close()
            if gateway:
                gateway.close()

    def test_dev_simulation_never_constructs_hardware_gateways(self):
        with patch.object(arm_gateway, 'ArmGateway', side_effect=AssertionError('real arm gateway')):
            options = server.parse_args(['--source', 'none', '--env', 'dev', '--simulate'])
            service, gateway = server.development_services(options)
            try:
                self.assertIsNone(gateway)
                self.assertEqual(service.status()['environment'], 'dev')
                self.assertEqual(service.status()['mode'], 'hardware')
                self.assertTrue(service.status()['arm']['simulated'])
            finally:
                service.close()

    def test_health_identity_includes_control_permissions_and_diagnostics(self):
        from test_arm_control import ArmOwner

        for options, mode, diagnostics in (({}, b'hardware', b'false'),
                                          ({'mode': 'readonly'}, b'readonly', b'false'),
                                          ({'environment': 'dev', 'arm_gateway': ArmOwner(),
                                            'dog_enabled': False}, b'hardware', b'true')):
            with self.subTest(mode=mode, diagnostics=diagnostics):
                self.control = control.ControlService(**options)
                try:
                    self.request('GET', '/healthz')
                    self.assertIn(b'X-Lite3-Control-Mode: ' + mode + b'\r\n', self.response_head)
                    self.assertIn(b'X-Lite3-Arm-Diagnostics: ' + diagnostics + b'\r\n', self.response_head)
                finally:
                    self.control.close()

    def test_invalid_debug_startup_is_rejected_before_service_creation(self):
        for flags in (['--simulate'], ['--env', 'dist', '--simulate'],
                      ['--control-mode', 'simulation'],
                      ['--env', 'dev', '--simulate', '--control-mode', 'readonly'],
                      ['--arm-motion-profile', '/tmp/record'],
                      ['--env', 'dev', '--simulate', '--arm-motion-profile', '/tmp/record']):
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    server.parse_args(['--source', 'none'] + flags)
                self.assertEqual(error.exception.code, 2)
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


    def test_status_and_control_responses_match_sdk_dictionary_shapes(self):
        from src.lite3_dev_server import AcquireResponse, ArmStatus, ControlStatus, DisabledStatus, DogStatus

        self.control = None
        status, body = self.request('GET', '/api/status')
        self.assertEqual((status, json.loads(body)), (200, {'mode': 'disabled'}))
        self.assertEqual(json.loads(body).keys(), DisabledStatus.__annotations__.keys())

        self.control = control.ControlService(mode='disabled')
        _, body = self.request('GET', '/api/status')
        self.assertEqual(json.loads(body).keys(), ControlStatus.__annotations__.keys())

        self.control = control.ControlService(environment='dev', simulate=True)
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

        self.control = control.ControlService(environment='dev', simulate=True)
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
        args = server.parse_args(['--source', 'none', '--env', 'dev', '--control-mode', 'hardware',
                                  '--arm-motion-profile', '/home/ysc/arm/session.json'])
        self.assertEqual(args.arm_motion_profile, '/home/ysc/arm/session.json')
        for options in ([], ['--env', 'dev', '--control-mode', 'readonly'],
                        ['--env', 'dev', '--control-mode', 'hardware', '--arm-host', 'robot']):
            with self.subTest(options=options), self.assertRaises(SystemExit):
                server.parse_args(['--source', 'none', '--arm-motion-profile', '/tmp/profile'] + options)


if __name__ == '__main__':
    unittest.main()
