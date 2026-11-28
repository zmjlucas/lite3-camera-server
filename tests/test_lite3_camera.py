"""Check that deployment preserves the server entry point on an upload error."""
from pathlib import Path
import unittest
from unittest.mock import patch

from src import launcher as client


class CameraCommandsTest(unittest.TestCase):
    def test_failed_runtime_dependency_upload_preserves_existing_entrypoint(self):
        uploaded = []
        installed = []

        def remote(command):
            if command[0] == "scp":
                name = Path(command[-2]).name
                uploaded.append(name)
                return 23 if name == "dev_client.js" else 0
            if "-tt" in command:
                installed.append(command)
            return 0

        with patch.object(client.subprocess, "call", side_effect=remote):
            self.assertEqual(client.main(["deploy", "robot", "--direct"]), 23)
        self.assertEqual(uploaded[-1], "dev_client.js")
        self.assertNotIn("server.py", uploaded)
        self.assertEqual(installed, [])


if __name__ == "__main__":
    unittest.main()
