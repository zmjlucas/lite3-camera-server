"""Real subprocess and HTTP checks for the reusable simulation context."""
import contextlib
import subprocess
import unittest
from unittest.mock import patch

from src.testing import simulated_server


@contextlib.contextmanager
def capture_children():
    """Record and clean up real children."""
    children = []
    popen = subprocess.Popen

    def start(arguments, **kwargs):
        child = popen(arguments, **kwargs)
        children.append(child)
        return child

    with patch('src.testing.subprocess.Popen', side_effect=start):
        try:
            yield children
        finally:
            # Prevent failed assertions from leaving test children behind.
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)


class SimulationTests(unittest.TestCase):
    def test_body_exception_propagates_after_child_is_reaped(self):
        for error in (LookupError, KeyboardInterrupt):
            with self.subTest(error=error), capture_children() as children:
                with self.assertRaisesRegex(error, 'caller failed'):
                    with simulated_server():
                        raise error('caller failed')
                self.assertIsNotNone(children[0].returncode)


if __name__ == '__main__':
    unittest.main()
