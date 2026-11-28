"""Exercise application orchestration independently of HTTP and device I/O."""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

from src import control
from src.application import ControlApplication
from src.remote.arm_worker import observed_state


class ApplicationTests(unittest.TestCase):
    def test_estop_during_arm_read_is_admitted_and_reconciled_after_io(self):
        reading = threading.Event()
        stopped = threading.Event()
        angles = {'b': 1., 's': 2., 'e': 3., 'w': 4.}

        class Gateway:
            def status(self):
                return {'connected': False, 'reconnect_required': False,
                        'motion_enabled': True, 'reason': None,
                        'commissioning': {'state': 'readonly', 'profile_loaded': False, 'nonce_consumed': False},
                        'cached_feedback': None}

            def read_state(self):
                reading.set()
                if not stopped.wait(2):
                    raise RuntimeError('Control stop blocked behind the arm read')
                return dict(observed_state(angles), motion_enabled=True, reason=None)

        service = control.ControlService()
        self.addCleanup(service.close)
        app = ControlApplication(service, Gateway())
        with ThreadPoolExecutor(max_workers=1) as pool:
            state_request = pool.submit(app.dispatch, 'arm/state', {})
            self.assertTrue(reading.wait(2))
            try:
                stopped_status = app.dispatch('control/estop', {})
                self.assertTrue(stopped_status['estop'])
                self.assertFalse(stopped_status['arm']['motion_enabled'])
            finally:
                stopped.set()
            state = state_request.result(timeout=2)
        self.assertEqual(state['position_deg'], angles)
        self.assertIs(state['observed'], True)
        self.assertFalse(state['motion_enabled'])
        self.assertIn('emergency stop', state['reason'].lower())
