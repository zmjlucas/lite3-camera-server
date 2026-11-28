"""Exercise application orchestration independently of HTTP and device I/O."""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

from src import arm_gateway, control
from src.application import ControlApplication
from src.remote.arm_worker import observed_state


class ApplicationTests(unittest.TestCase):
    def service(self, **kwargs):
        service = control.ControlService(**kwargs)
        self.addCleanup(service.close)
        return service

    def test_acquired_simulation_and_unobserved_arm_summary(self):
        app = ControlApplication(self.service(environment="dev", simulate=True))
        reply = app.dispatch('control/acquire', {'owner': 'test'})
        self.assertTrue(reply['status']['lease_active'])
        self.assertTrue(app.status()['lease_active'])
        for action in ('arm/state', 'arm/reconnect'):
            state = app.dispatch(action, {})
            self.assertIs(state['observed'], False)
            self.assertTrue(state['simulated'])
            self.assertEqual(state['position_deg'], {'b': 0., 's': 0., 'e': 0., 'w': 0., 'h': 0.})

    def test_disabled_and_invalid_requests_raise_semantic_errors(self):
        with self.assertRaises(TypeError):
            ControlApplication()
        disabled = ControlApplication(self.service(mode='disabled'))
        self.assertEqual(disabled.status()['mode'], 'disabled')
        self.assertFalse(disabled.status()['dog']['motion_enabled'])
        self.assertFalse(disabled.status()['arm']['motion_enabled'])
        app = ControlApplication(self.service(environment="dev", simulate=True))
        for target, action, body, code in (
                (disabled, 'arm/state', {}, 'conflict'),
                (app, 'arm/state', {'raw': 1}, 'invalid_request'),
                (app, 'arm/state', [], 'invalid_request'),
                (app, 'arm/operation', {'id': 'missing'}, 'not_found'),
                (app, 'unregistered', {}, 'not_found')):
            with self.subTest(action=action), self.assertRaises(control.ControlError) as error:
                target.dispatch(action, body)
            self.assertEqual(error.exception.code, code)

    def test_estop_during_arm_read_is_admitted_and_reconciled_after_io(self):
        reading = threading.Event()
        stopped = threading.Event()
        angles = {'b': 1., 's': 2., 'e': 3., 'w': 4.}

        class Gateway:
            def read_state(self):
                reading.set()
                if not stopped.wait(2):
                    raise RuntimeError('Control stop blocked behind the arm read')
                return dict(observed_state(angles), motion_enabled=True, reason=None)

        app = ControlApplication(self.service(), Gateway())
        with ThreadPoolExecutor(max_workers=1) as pool:
            state_request = pool.submit(app.dispatch, 'arm/state', {})
            self.assertTrue(reading.wait(2))
            try:
                self.assertTrue(app.dispatch('control/estop', {})['estop'])
            finally:
                stopped.set()
            state = state_request.result(timeout=2)
        self.assertEqual(state['position_deg'], angles)
        self.assertIs(state['observed'], True)
        self.assertFalse(state['motion_enabled'])
        self.assertIn('emergency stop', state['reason'].lower())

    def test_gateway_transport_failure_is_reported_as_unavailable(self):
        class UnavailableGateway:
            def read_state(self):
                raise arm_gateway.ArmGatewayError('Arm read failed; explicit reconnect required.')

        app = ControlApplication(self.service(mode='readonly'), UnavailableGateway())
        with self.assertRaisesRegex(control.ControlError, 'explicit reconnect') as error:
            app.dispatch('arm/state', {})
        self.assertEqual(error.exception.code, 'unavailable')

    def test_gateway_reconnect_preserves_disconnected_result(self):
        gateway = arm_gateway.ArmGateway()
        self.addCleanup(gateway.close)
        app = ControlApplication(self.service(mode='readonly'), gateway)
        reply = app.dispatch('arm/reconnect', {})
        self.assertEqual((reply['connected'], reply['reconnect_required'], reply['motion_enabled']),
                         (False, False, False))
