"""Exercise partial startup cleanup without binding sockets or opening devices."""
import contextlib
import io
import os
import unittest
from unittest.mock import MagicMock, patch

from src import server


class StartupTests(unittest.TestCase):
    def test_normal_and_partial_startup_release_each_gateway_once(self):
        for profile in ((False, True) if os.name == 'posix' else (False,)):
            for failure in ('bind', 'capture', None):
                with self.subTest(commissioning=profile, failure=failure):
                    args = ['--control-mode', 'hardware' if profile else 'readonly']
                    gateway_type = 'src.arm_motion.ArmMotionGateway' if profile else 'src.arm_gateway.ArmGateway'
                    if profile:
                        args += ['--env', 'dev', '--arm-motion-profile', 'local-profile']
                    gateway = MagicMock()
                    http = MagicMock()
                    if failure == 'bind':
                        http.side_effect = OSError('bind failed')
                    def stop_after_request():
                        signal_number, on_signal = signal_handler.call_args[0]
                        on_signal(signal_number, None)

                    http.return_value.handle_request.side_effect = stop_after_request
                    with patch(gateway_type, return_value=gateway), patch.object(server.signal, 'signal') as signal_handler, \
                            patch.object(server, 'CameraHTTPServer', http), \
                            patch.object(server, 'CaptureController') as controller, \
                            contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                        if failure == 'bind':
                            self.assertEqual(server.main(args), 1)
                        elif failure == 'capture':
                            controller.return_value.start.side_effect = RuntimeError('capture failed')
                            with self.assertRaisesRegex(RuntimeError, 'capture failed'):
                                server.main(args)
                        else:
                            self.assertEqual(server.main(args), 0)
                        controller.return_value.shutdown.assert_called_once_with()
                        if failure != 'bind':
                            http.return_value.__exit__.assert_called_once()
                    gateway.close.assert_called_once_with()
