"""Copied outside the checkout and run with the wheel's isolated interpreter."""
from importlib.util import find_spec
from pathlib import Path
import queue
import subprocess
import sys
import threading

import lite3_dev_server
from lite3_dev_server import DevClient, DevClientError


def main():
    package = Path(lite3_dev_server.__file__).resolve().parent
    assert Path(sys.prefix).resolve() in package.parents, package
    assert (package / 'py.typed').is_file()
    assert find_spec('server') is None
    assert not {'cv2', 'numpy', 'serial', 'server', 'control'} & set(sys.modules)
    server_path = Path(sys.argv[1]).resolve()
    # Hide the installed SDK from the device while keeping sibling imports available.
    process = subprocess.Popen(
        [sys.executable, '-E', '-S', '-B', '-u', str(server_path), '--source', 'none',
         '--env', 'dev', '--simulate', '--bind', '127.0.0.1', '--port', '0'],
        cwd=str(server_path.parent), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines = queue.Queue()
    threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
    try:
        origin = lines.get(timeout=10).strip()
        if not origin.startswith('http://127.0.0.1:'):
            raise RuntimeError('Simulation failed to start: ' + process.stderr.read())
        with DevClient(origin) as robot:
            status = robot.status()
            assert status['mode'] == 'hardware'
            assert status['environment'] == 'dev'
            assert status['dog']['simulated']
            acquired = robot.acquire('installed-wheel')
            assert acquired['status']['lease_active']
            observer = DevClient(origin)
            try:
                observer.acquire('conflicting-wheel-client')
            except DevClientError as error:
                assert error.status == 409
            else:
                raise AssertionError('Second client acquired an occupied lease')
            robot.arm_jog('e', 1)
            assert robot.arm_state()['position_deg']['e'] == 1
            robot.heartbeat()
            assert not robot.arm_stop()['lease_active']
            assert robot.lease is None
            assert robot.estop()['estop']
            revision = robot.status()['safety_revision']
            assert not robot.reset(revision)['estop']
        print('PASS: installed SDK imports, types marker, HTTP lifecycle and error status')
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


if __name__ == '__main__':
    main()
