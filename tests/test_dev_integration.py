"""Run exactly the deployment bundle against loopback, without cameras or robots."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

from src.launcher import SERVER, SERVER_BUNDLE
from src.lite3_dev_server import DevClient, DevClientError


class DeployedDevTests(unittest.TestCase):
    def test_deployed_entrypoint_sdk_watchdog_and_latch_without_camera(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for source, _ in SERVER_BUNDLE:
                destination = root / source.relative_to(SERVER.parent)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read_bytes())
            process = subprocess.Popen([sys.executable,'-B','-u',str(root/'server.py'),
                                        '--source','none','--bind','127.0.0.1','--port','0'],
                                       stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=directory)
            lines = queue.Queue()
            threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
            try:
                origin = lines.get(timeout=5).strip().rstrip('/')
                self.assertTrue(origin.startswith('http://127.0.0.1:'), origin)
                reader = build_opener(ProxyHandler({}))
                with reader.open(origin+'/control', timeout=2) as response:
                    self.assertIn(b'Lite3 Dev Server', response.read())
                with self.assertRaises(HTTPError) as missing:
                    reader.open(origin+'/healthz', timeout=2)
                self.assertEqual(missing.exception.code,503)
                missing.exception.close()
                with DevClient(origin) as robot:
                    self.assertEqual(robot.status()['mode'],'simulation')
                    first = robot.acquire()
                    robot.arm_jog('e',1)
                    self.assertEqual(robot.arm_state()['position_deg']['e'],1)
                    self.assertNotIn(first['lease'],json.dumps(robot.status()))
                    # No heartbeat: the actual worker must revoke ownership by itself.
                    deadline = time.monotonic()+3
                    while robot.status()['lease_active'] and time.monotonic()<deadline:
                        time.sleep(.05)
                    self.assertFalse(robot.status()['lease_active'])
                    with self.assertRaises(DevClientError):
                        robot.arm_jog('e',1)
                    robot.estop()
                    with self.assertRaises(DevClientError) as latched:
                        robot.acquire()
                    self.assertEqual(latched.exception.status,423)
                    robot.reset(robot.status()['safety_revision'])
                    robot.acquire()
                    robot.stop()
                    self.assertIsNone(robot.lease)
                    self.assertEqual(robot.status()['dog']['phase'],'disarmed')
            finally:
                process.terminate()
                try:
                    _, error = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    _, error = process.communicate()
            if os.name != "nt":  # Windows terminate is forced; POSIX exercises signal cleanup.
                self.assertEqual(process.returncode,0,error)

if __name__ == '__main__':
    unittest.main()
