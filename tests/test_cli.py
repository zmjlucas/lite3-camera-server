"""Offline checks for explicit CLI authorization."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from src.lite3_dev_server import cli


class CLIOfflineTests(unittest.TestCase):
    def invoke(self, arguments):
        output, diagnostic = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(diagnostic):
            with patch('socket.socket', side_effect=AssertionError('Offline command used network')):
                code = cli.main(arguments)
        return code, output.getvalue(), diagnostic.getvalue()

    def test_mutation_requires_full_execute_flag_at_every_parser_level(self):
        command = ['dog', 'forward', '--speed', '.1', '--duration', '1', '--transport', 'simulated']
        cases = [command, ['--exec'] + command, ['dog', '--exec'] + command[1:],
                 command + ['--exec'], command + ['--execut'],
                 ['arm', 'jog', 'e', '--delta', '1', '--speed', '1', '--transport', 'simulated'],
                 ['reset', '--revision', 'observed', '--transport', 'simulated']]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.assert_argument_error(arguments)

    def assert_argument_error(self, arguments):
        code, output, diagnostic = self.invoke(arguments)
        self.assertEqual(code, 2, (arguments, output, diagnostic))
        result = json.loads(output)
        self.assertFalse(result['ok'])
        self.assertEqual(result['kind'], 'argument_error')
        self.assertTrue(result['error'])
        self.assertTrue(diagnostic)
        self.assertNotIn('Traceback', diagnostic)


if __name__ == '__main__':
    unittest.main()
