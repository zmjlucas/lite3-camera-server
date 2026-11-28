"""Exercise installed console commands and deployment files outside the checkout."""
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from urllib.request import ProxyHandler, build_opener

from lite3_dev_server import DevClient


def main():
    scripts = Path(sys.executable).parent
    suffix = '.exe' if os.name == 'nt' else ''
    simulation = scripts / ('lite3-sim' + suffix)
    launcher = scripts / ('lite3-launcher' + suffix)
    assert simulation.is_file(), 'The wheel must install lite3-sim'
    assert launcher.is_file(), 'The wheel must install lite3-launcher'

    for command in ('deploy', 'doctor', 'view'):
        result = subprocess.run(
            [str(launcher), command, 'ysc@192.168.1.103', '--direct', '--dry-run'],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        assert 'ssh' in result.stdout, result.stdout

    process = subprocess.Popen([str(simulation), '--port', '0'], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
    lines = queue.Queue()
    threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
    try:
        url = lines.get(timeout=10).strip()
        assert url.startswith('http://127.0.0.1:'), url
        status = DevClient(url).status()
        assert status['environment'] == 'dev'
        assert status['dog']['simulated'] and status['arm']['simulated']
        reader = build_opener(ProxyHandler({}))
        for path, content in (('/', b'<html'), ('/control', b'Lite3 Dev Server'),
                              ('/static/dev_client.js', b'DevClient')):
            with reader.open(url.rstrip('/') + path, timeout=2) as response:
                assert content in response.read(), path
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()

    # Read the installed bundle and run it with site packages hidden, as on a device.
    from lite3_dev_server_tools.launcher import SERVER, SERVER_BUNDLE
    from lite3_dev_server_tools.testing import simulated_server
    with simulated_server() as url, DevClient(url) as robot:
        assert robot.status()['dog']['simulated']
        robot.acquire('installed-context')
        robot.arm_jog('e', 1)
        assert robot.arm_state()['position_deg']['e'] == 1
    with tempfile.TemporaryDirectory(prefix='lite3-installed-deploy-') as directory:
        deployed = Path(directory)
        for source, _ in SERVER_BUNDLE:
            destination = deployed / source.relative_to(SERVER.parent)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(source), str(destination))
        subprocess.run([sys.executable, '-I', '-B',
                        str(Path(__file__).with_name('installed_sdk_smoke.py')),
                        str(deployed / 'server.py')], check=True, timeout=30)
    print('PASS: installed commands, HTTP assets, launcher previews, simulation context and deployment bundle')


if __name__ == '__main__':
    main()
