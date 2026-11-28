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
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from src.launcher import SERVER_BUNDLE
from src.lite3_dev_server import DevClient, DevClientError
from src.remote import camera_doctor


class DeployedDevTests(unittest.TestCase):
    def test_deployed_entrypoint_sdk_watchdog_and_latch_without_camera(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for source, folder in SERVER_BUNDLE:
                destination = root / Path(folder).relative_to('lite3-dev-server') / source.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read_bytes())
            process = subprocess.Popen([sys.executable,'-B','-u',str(root/'server.py'),
                                        '--source','none','--bind','127.0.0.1','--port','0',
                                        '--env','dev','--simulate'],
                                       stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=directory)
            lines = queue.Queue()
            threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
            try:
                origin = lines.get(timeout=5).strip().rstrip('/')
                self.assertTrue(origin.startswith('http://127.0.0.1:'), origin)
                reader = build_opener(ProxyHandler({}))
                # The standalone doctor consumes a passive snapshot from the deployed bundle.
                with patch.object(camera_doctor, 'run', return_value={'state': 'unverified', 'output': ''}), \
                        patch.object(camera_doctor.camera_sources, 'discover_sources', return_value=[]):
                    report = camera_doctor.collect_report({'remote_port': urlsplit(origin).port,
                                                           'install_dir': str(root)})
                self.assertEqual(report['running']['state'], 'observed')
                self.assertTrue(report['installed']['matches_running'])
                self.assertEqual(report['udp']['state'], 'not_applicable')
                self.assertEqual(report['serial']['state'], 'not_applicable')
                with reader.open(origin+'/control', timeout=2) as response:
                    self.assertIn(b'Lite3 Dev Server', response.read())
                    self.assertEqual(response.headers['X-Lite3-Environment'], 'dev')
                    self.assertEqual(response.headers['X-Lite3-Simulated'], 'true')
                with self.assertRaises(HTTPError) as missing:
                    reader.open(origin+'/healthz', timeout=2)
                self.assertEqual(missing.exception.code,503)
                missing.exception.close()
                with DevClient(origin) as robot:
                    self.assertEqual(robot.status()['mode'], 'hardware')
                    self.assertEqual(robot.status()['environment'], 'dev')
                    self.assertTrue(robot.status()['dog']['simulated'])
                    first = robot.acquire()
                    robot.arm_jog('e',1)
                    self.assertEqual(robot.arm_state()['position_deg']['e'],1)
                    self.assertNotIn(first['lease'],json.dumps(robot.status()))
                    # Stop renewing the lease and check that the server revokes it.
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
