"""Public SDK imports and inlined PEP 561 annotations."""
import importlib.util
from pathlib import Path
from typing import Optional, get_type_hints
import unittest


class SDKPackageTests(unittest.TestCase):
    def test_sdk_package_includes_type_marker(self):
        spec = importlib.util.find_spec('src.lite3_dev_server')
        self.assertIsNotNone(spec, 'Expected an SDK package under src')

        self.assertTrue((Path(spec.origin).parent / 'py.typed').is_file())

    def test_public_methods_expose_response_and_argument_types(self):
        spec = importlib.util.find_spec('src.lite3_dev_server')
        self.assertIsNotNone(spec, 'Expected an SDK package under src')

        from src.lite3_dev_server import (
            AcquireResponse, ArmJoint, ArmOperation, ArmReconnectResponse, ArmState, ControlStatus,
            DevClient, DevClientError,
        )

        for name, response in {
            'acquire': AcquireResponse, 'arm_jog': ControlStatus, 'arm_state': ArmState,
            'arm_reconnect': ArmReconnectResponse, 'estop': ControlStatus, 'heartbeat': ControlStatus,
            'release': Optional[ControlStatus], 'reset': ControlStatus, 'stand': ControlStatus,
            'status': ControlStatus, 'stop': ControlStatus, 'velocity': ControlStatus,
            'arm_operation': ArmOperation, 'arm_stop': ControlStatus,
        }.items():
            with self.subTest(method=name):
                self.assertIs(get_type_hints(getattr(DevClient, name))['return'], response)
        self.assertIs(get_type_hints(DevClient.arm_jog)['joint'], ArmJoint)
        self.assertIs(get_type_hints(DevClient.reset)['revision'], str)
        self.assertEqual(get_type_hints(DevClientError)['status'], Optional[int])

    def test_protocol_and_error_contract_are_public(self):
        from src.lite3_dev_server import ErrorResponse, PROTOCOL_HEADER, PROTOCOL_VERSION

        self.assertEqual(PROTOCOL_HEADER, 'X-Lite3-Dev-Protocol')
        self.assertEqual(PROTOCOL_VERSION, '1')
        self.assertEqual(get_type_hints(ErrorResponse), {'error': str, 'code': str})


if __name__ == '__main__':
    unittest.main()
