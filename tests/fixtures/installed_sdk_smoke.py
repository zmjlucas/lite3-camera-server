"""Copied outside the checkout and run with the wheel's isolated interpreter."""
from importlib.util import find_spec
from importlib.metadata import version
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from urllib.request import ProxyHandler, build_opener

import lite3_dev_server
from lite3_dev_server import (
    ArmJog, CameraClient, CameraFrameUnavailable, DevClient, DevClientError,
    ManagedClient, PreflightError, validate_dog_action,
)


def check_cli(origin):
    executable = Path(sys.executable).with_name('lite3-control.exe' if os.name == 'nt' else 'lite3-control')
    help_result = subprocess.run([str(executable), '--help'], capture_output=True, text=True, timeout=5)
    assert help_result.returncode == 0, help_result.stderr
    assert '--execute' in help_result.stdout
    module_help = subprocess.run([sys.executable, '-I', '-B', '-m', 'lite3_dev_server', '--help'],
                                 capture_output=True, text=True, timeout=5)
    assert module_help.returncode == 0, module_help.stderr

    def invoke(arguments, expected=0):
        process = subprocess.run([str(executable), '--url', origin] + arguments,
                                 capture_output=True, text=True, timeout=20)
        assert process.returncode == expected, process.stderr
        result = json.loads(process.stdout)
        assert result['ok'] == (expected == 0), result
        return result

    result = invoke([])
    assert result['command'] == 'status'
    assert not result['result']['lease_active']
    assert result['result']['dog']['simulated']
    action = ['dog', 'forward', '--speed', '.1', '--duration', '.1']
    assert invoke(action + ['--transport', 'simulated'], 2)['kind'] == 'argument_error'
    assert invoke(action + ['--transport', 'real', '--execute'], 1)['kind'] == 'execution_error'
    preview = invoke(action + ['--url', 'http://offline.invalid', '--transport', 'simulated', '--dry-run'])
    assert preview['result']['dry_run']
    result = invoke(action + ['--transport', 'simulated', '--execute'])
    assert result['result']['dog']['action']['phase'] == 'completed'
    assert not result['result']['lease_active']
    initial_elbow = DevClient(origin).arm_state()['position_deg']['e']
    result = invoke(['arm', 'jog', 'e', '--delta', '1', '--speed', '2',
                     '--transport', 'simulated', '--execute'])
    assert result['result']['arm']['position_deg']['e'] == initial_elbow + 1
    assert not invoke([])['result']['lease_active']


def main():
    package = Path(lite3_dev_server.__file__).resolve().parent
    assert Path(sys.prefix).resolve() in package.parents, package
    assert (package / 'py.typed').is_file()
    expected = json.loads(Path('expected-build.json').read_text(encoding='utf-8'))
    assert version('lite3-dev-server') == lite3_dev_server.__version__ == expected['version']
    assert dict(lite3_dev_server.BUILD_INFO._asdict()) == {
        key: expected[key] for key in lite3_dev_server.BuildInfo._fields}
    assert not (package / 'previous_release.py').exists(), 'Upgrade left the old distribution installed'
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
        deployed = json.loads((server_path.parent / '_build.json').read_text(encoding='utf-8'))
        with build_opener(ProxyHandler({})).open(origin + '/api/status', timeout=3) as response:
            assert response.headers['X-Lite3-Release'] == expected['version']
            assert response.headers['X-Lite3-Source-Commit'] == (expected['source_commit'] or 'unknown')
            assert response.headers['X-Lite3-Build'] == deployed['build_id']
        camera = CameraClient(origin)
        health = camera.health()
        assert health['source'] == 'none'
        assert health['status'] == 'starting'
        assert health['frame_age_s'] is None
        assert camera.sources() == {'sources': [], 'selected': None}
        for read_frame in (camera.snapshot, camera.stream):
            try:
                read_frame()
            except CameraFrameUnavailable as error:
                assert error.status == 503
                assert error.health == health
            else:
                raise AssertionError('Camera without capture returned a frame')
        assert validate_dog_action('turn_left', .1, 1.) == {'vx': 0., 'vy': 0., 'yaw': -.1}
        with DevClient(origin, target='hardware') as wrong_target:
            assert not wrong_target.preflight('acquire')['ok']
            try:
                wrong_target.acquire()
            except PreflightError as error:
                assert error.status is None and error.code is None
                assert error.result['reasons'][0]['code'] == 'transport_mismatch'
            else:
                raise AssertionError('Hardware target acquired simulated service')
        with DevClient(origin, target='simulation') as robot:
            status = robot.status()
            assert status['mode'] == 'hardware'
            assert status['environment'] == 'dev'
            assert status['dog']['simulated']
            acquired = robot.acquire('installed-wheel')
            assert acquired['status']['lease_active']
            assert robot.preflight('dog/stand', status=acquired['status'])['ok']
            assert not robot.preflight('dog/action', status=acquired['status'])['ok']
            try:
                robot.dog_action_status('unknown-action')
            except DevClientError as error:
                assert error.status == 404
            else:
                raise AssertionError('Unknown dog action resolved')
            assert robot.lease == acquired['lease']
            observer = DevClient(origin)
            try:
                observer.acquire('conflicting-wheel-client')
            except DevClientError as error:
                assert error.status == 409
                assert error.code == 'conflict'
            else:
                raise AssertionError('Second client acquired an occupied lease')
            robot.arm_jog('e', 1)
            assert robot.arm_state()['position_deg']['e'] == 1
            accepted = robot.arm_action({'b': 5, 'h': -3}, duration=.15)
            operation = accepted['arm']['operation']
            assert operation is not None
            result = robot.wait_arm_operation(operation['operation_id'], timeout=3)
            assert result['phase'] == 'completed'
            assert robot.arm_state()['position_deg'] == dict(b=5., s=0., e=1., w=0., h=-3.)
            robot.stand()
            deadline = time.monotonic() + 10
            while not robot.preflight('dog/action')['ok']:
                assert time.monotonic() < deadline, 'Preparation timed out'
                robot.heartbeat()
                time.sleep(.1)
            assert robot.forward(.05, .2)['dog']['action']['phase'] == 'running'
            assert not robot.arm_stop()['lease_active']
            assert robot.lease is None
            assert robot.estop()['estop']
            revision = robot.status()['safety_revision']
            assert not robot.reset(revision)['estop']
        with ManagedClient(origin, owner='installed-managed-wheel') as managed:
            result = managed.run_sequence(
                [ArmJog('e', delta=1.0), ArmJog('e', delta=-0.5)],
                policy='completed_only', timeout=10,
            )
            assert result.phase == 'completed', result
            assert isinstance(result.results, tuple)
            assert len(result.results) == 2
            for step in result.results:
                assert step.phase == 'completed', step
                assert step.action_id is None
                assert step.operation is None
                assert step.cleanup_error is None, step
                assert step.status is not None
            assert result.results[0].status['arm']['position_deg']['e'] == 2
            assert result.results[1].status['arm']['position_deg']['e'] == 1.5
        with DevClient(origin) as observer:
            assert observer.arm_state()['position_deg']['e'] == 1.5
            assert not observer.status()['lease_active']
        check_cli(origin)
        print('PASS: installed SDK provenance, camera, preflight, actions, managed sequence and CLI')
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
