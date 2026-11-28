"""Public SDK imports and inlined PEP 561 annotations."""
import importlib.util
from pathlib import Path
import sys
from typing import Optional, get_args, get_type_hints
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


class SDKPackageTests(unittest.TestCase):
    def test_sdk_package_includes_type_marker(self):
        spec = importlib.util.find_spec('lite3_dev_server')
        self.assertIsNotNone(spec, 'Expected an SDK package under src')

        self.assertTrue((Path(spec.origin).parent / 'py.typed').is_file())

    def test_public_methods_expose_response_and_argument_types(self):
        spec = importlib.util.find_spec('lite3_dev_server')
        self.assertIsNotNone(spec, 'Expected an SDK package under src')

        from lite3_dev_server import (
            AcquireResponse, ArmJoint, ArmOperation, ArmReconnectResponse, ArmState, ControlStatus,
            DevClient, DevClientError,
        )

        for name, response in {
            'acquire': AcquireResponse, 'arm_jog': ControlStatus, 'arm_state': ArmState,
            'arm_reconnect': ArmReconnectResponse, 'estop': ControlStatus, 'heartbeat': ControlStatus,
            'release': Optional[ControlStatus], 'reset': ControlStatus, 'stand': ControlStatus,
            'status': ControlStatus, 'stop': ControlStatus, 'velocity': ControlStatus,
            'arm_operation': ArmOperation, 'arm_stop': ControlStatus,
            'arm_action': ControlStatus, 'wait_arm_operation': ArmOperation,
        }.items():
            with self.subTest(method=name):
                self.assertIs(get_type_hints(getattr(DevClient, name))['return'], response)
        self.assertIs(get_type_hints(DevClient.arm_jog)['joint'], ArmJoint)
        self.assertIs(get_type_hints(DevClient.reset)['revision'], str)
        self.assertEqual(get_type_hints(DevClientError)['status'], Optional[int])
        self.assertEqual(get_type_hints(DevClientError)['code'], Optional[str])

    def test_preflight_and_offline_rules_are_public_typed_interfaces(self):
        import lite3_dev_server as sdk

        for name in ('TargetTransport', 'MotionOperation', 'PreflightReason', 'PreflightResult',
                     'PreflightError', 'DogActionCapability', 'DogCapabilities', 'check_preflight',
                     'validate_dog_action', 'validate_velocity', 'validate_arm_jog'):
            self.assertIn(name, sdk.__all__)
        self.assertIs(get_type_hints(sdk.DevClient.preflight)['return'], sdk.PreflightResult)
        self.assertIs(get_type_hints(sdk.check_preflight)['target'], sdk.TargetTransport)
        self.assertEqual(get_type_hints(sdk.DevClient.__init__)['target'], Optional[sdk.TargetTransport])

    def test_dog_action_contracts_are_exported_and_preserve_typed_results(self):
        import lite3_dev_server as sdk

        for name in ('DogActionName', 'DogActionStatus', 'DogActionRequest', 'DogFeedback'):
            self.assertTrue(hasattr(sdk, name), name)
            self.assertIn(name, sdk.__all__)
        for name in ('dog_action', 'forward', 'backward', 'turn_left', 'turn_right'):
            hints = get_type_hints(getattr(sdk.DevClient, name))
            self.assertIs(hints['return'], sdk.ControlStatus)
            self.assertIs(hints['speed'], float)
            self.assertIs(hints['duration'], float)
        self.assertIs(get_type_hints(sdk.DevClient.dog_action)['name'], sdk.DogActionName)
        dog = get_type_hints(sdk.DogStatus)
        self.assertIs(dog['feedback'], sdk.DogFeedback)
        self.assertEqual(dog['action'], Optional[sdk.DogActionStatus])
        self.assertIs(dog['actions_enabled'], bool)
        request = get_type_hints(sdk.DogActionRequest)
        self.assertIs(request['name'], sdk.DogActionName)
        self.assertIs(request['lease'], str)
        self.assertIs(request['seq'], int)

    def test_protocol_and_error_contract_are_public(self):
        from lite3_dev_server import ErrorResponse, PROTOCOL_HEADER, PROTOCOL_VERSION

        self.assertEqual(PROTOCOL_HEADER, 'X-Lite3-Dev-Protocol')
        self.assertEqual(PROTOCOL_VERSION, '1')
        self.assertEqual(get_type_hints(ErrorResponse), {'error': str, 'code': str})

    def test_full_control_status_contract_preserves_environment_and_simulation(self):
        from lite3_dev_server import ControlMode, ControlStatus, DogStatus, Environment

        self.assertEqual(get_args(Environment), ('dev', 'dist'))
        self.assertEqual(get_args(ControlMode), ('disabled', 'readonly', 'hardware'))
        self.assertIs(get_type_hints(ControlStatus)['environment'], Environment)
        self.assertIs(get_type_hints(ControlStatus)['dog'], DogStatus)
        self.assertIs(get_type_hints(DogStatus)['simulated'], bool)


if __name__ == '__main__':
    unittest.main()
