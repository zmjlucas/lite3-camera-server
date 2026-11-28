"""Public SDK imports and inlined PEP 561 annotations."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Optional, get_type_hints
import unittest


SRC = Path(__file__).resolve().parents[1] / 'src'


class SDKPackageTests(unittest.TestCase):
    def test_checkout_imports_reexport_the_packaged_sdk(self):
        spec = importlib.util.find_spec('src.lite3_dev_server')
        self.assertIsNotNone(spec, 'Expected an SDK package under src')

        self.assertTrue((Path(spec.origin).parent / 'py.typed').is_file())

        result = subprocess.run([sys.executable, '-B', '-c',
                                 'from pathlib import Path\n'
                                 'import sys\n'
                                 'sys.path.insert(0, str(Path.cwd().parent))\n'
                                 'from lite3_dev_server import DevClient, DevClientError\n'
                                 'import dev_client\n'
                                 'from src import dev_client as namespaced\n'
                                 'assert dev_client.DevClient is DevClient\n'
                                 'assert dev_client.DevClientError is DevClientError\n'
                                 'assert namespaced.DevClient is DevClient\n'
                                 'assert namespaced.DevClientError is DevClientError\n'],
                                cwd=str(SRC), capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_file_import_loads_the_sibling_sdk_outside_the_checkout(self):
        code = (
            'from importlib.util import module_from_spec, spec_from_file_location\n'
            'import sys\n'
            'for name in ("_lite3_dev_sdk", "dev_client", "external.sdk"):\n'
            '    spec = spec_from_file_location(name, sys.argv[1])\n'
            '    module = module_from_spec(spec)\n'
            '    spec.loader.exec_module(module)\n'
            '    assert module.DevClient.__module__.endswith("lite3_dev_server.client")\n'
            '    assert module.DevClientError.__module__ == module.DevClient.__module__\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(SRC / 'dev_client.py')],
                                    cwd=directory, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_public_methods_expose_response_and_argument_types(self):
        spec = importlib.util.find_spec('src.lite3_dev_server')
        self.assertIsNotNone(spec, 'Expected an SDK package under src')

        from src.lite3_dev_server import (
            AcquireResponse, ArmJoint, ArmReconnectResponse, ArmState, ControlStatus,
            DevClient, DevClientError, StatusResponse,
        )

        for name, response in {
            'acquire': AcquireResponse, 'arm_jog': ControlStatus, 'arm_state': ArmState,
            'arm_reconnect': ArmReconnectResponse, 'estop': ControlStatus, 'heartbeat': ControlStatus,
            'release': Optional[ControlStatus], 'reset': ControlStatus, 'stand': ControlStatus,
            'status': StatusResponse, 'stop': ControlStatus, 'velocity': ControlStatus,
        }.items():
            with self.subTest(method=name):
                self.assertIs(get_type_hints(getattr(DevClient, name))['return'], response)
        self.assertIs(get_type_hints(DevClient.arm_jog)['joint'], ArmJoint)
        self.assertIs(get_type_hints(DevClient.reset)['revision'], str)
        self.assertEqual(get_type_hints(DevClientError)['status'], Optional[int])

    def test_dog_action_contracts_are_exported_and_preserve_typed_results(self):
        from src import lite3_dev_server as sdk

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


if __name__ == '__main__':
    unittest.main()
