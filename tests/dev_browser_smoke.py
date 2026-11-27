"""Exercise the real control page in Chrome against a loopback-only fake API."""
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time

from camera_browser_smoke import find_browser


OBSERVER = r"""
<script>
(async () => {
  const $ = id => document.getElementById(id);
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
  const assert = (value, message) => { if (!value) throw new Error(message); };
  async function until(check, label) {
    for (let i = 0; i < 100; i++) { if (await check()) return; await delay(50); }
    throw new Error('Timed out: ' + label);
  }
  const calls = () => fetch('/test/calls').then(response => response.json());
  const count = async action => (await calls()).filter(row => row.action === action).length;
  async function ready() {
    $('acquire').click();
    await until(() => !$('stand').disabled, 'acquired');
    $('stand').click();
    await until(() => !document.querySelector('[data-axis="vx"]').disabled, 'stand ready');
  }
  const press = () => document.querySelector('[data-axis="vx"]').dispatchEvent(
    new PointerEvent('pointerdown', {bubbles: true, pointerId: 1, button: 0}));
  const release = () => window.dispatchEvent(new PointerEvent('pointerup', {pointerId: 1}));
  let result;
  try {
    await until(() => $('connection').textContent.includes('在线'), 'status');
    $('token').value = 'browser-test-secret'; $('save-token').click();
    assert($('token').value === '', 'token entry must clear after applying');
    assert(localStorage.length === 0 && sessionStorage.length === 0, 'token must stay out of storage');
    await ready();
    press();
    await until(async () => await count('dog/velocity') === 1, 'first velocity');
    release();
    await until(async () => await count('dog/stop') >= 1, 'release stop');
    await delay(800);
    assert(await count('dog/velocity') === 1, 'late velocity reply restarted held motion');
    assert($('stand').disabled, 'release must forget lease');
    const acquisitions = await count('control/acquire');
    await delay(800);
    assert(await count('control/acquire') === acquisitions, 'page automatically reacquired control');

    await ready(); press();
    await until(async () => await count('dog/velocity') >= 2, 'second hold');
    window.dispatchEvent(new Event('blur'));
    const afterBlur = await count('dog/velocity');
    await delay(800);
    assert(await count('dog/velocity') === afterBlur, 'blur left a velocity timer running');
    assert($('stand').disabled, 'blur must forget lease');

    await ready();
    await fetch('/test/fail-next', {method: 'POST'});
    press();
    await until(() => $('notice').textContent.includes('失败'), 'HTTP failure shown');
    const afterFailure = await count('dog/velocity');
    await delay(800);
    assert(await count('dog/velocity') === afterFailure, 'HTTP error resumed motion');
    assert($('stand').disabled, 'HTTP error must forget lease');

    await fetch('/test/delay-acquire', {method: 'POST'});
    $('acquire').click();
    await until(async () => await count('control/acquire') === acquisitions + 3, 'pending acquire');
    window.dispatchEvent(new Event('blur'));
    await delay(850);
    assert($('stand').disabled, 'late acquisition after blur restored control');
    assert(await count('control/release') >= 1, 'late acquisition was not released');
    await until(() => !$('camera-frame').hidden, 'JPEG preview');
    await fetch('/test/camera-offline', {method: 'POST'});
    await until(() => $('camera-frame').hidden, 'stale camera frame hidden');
    await fetch('/test/dog-error', {method: 'POST'});
    await until(() => !$('reset').disabled, 'UDP fault can be reset without estop');
    $('reset').click();
    await until(async () => await count('control/reset') === 1, 'explicit fault reset');
    assert($('stand').disabled, 'fault reset must not restore ownership');
    await fetch('/test/delay-status', {method: 'POST'});
    await until(async () => await count('delayed-status') === 1, 'stale status in flight');
    await ready();
    await delay(900);
    assert($('owner-state').textContent.includes('本页持有'), 'pre-acquisition status discarded a new lease');
    $('release').click();
    await until(() => !$('acquire').disabled, 'released before observer arm read');
    await fetch('/test/delay-arm-failure', {method: 'POST'});
    $('arm-state').click();
    await until(async () => await count('arm/state') === 1, 'observer arm read in flight');
    await ready();
    const stopsBeforeArmFailure = await count('dog/stop');
    await delay(900);
    assert($('owner-state').textContent.includes('本页持有'), 'late observer arm error revoked new lease');
    assert(await count('dog/stop') === stopsBeforeArmFailure, 'late observer arm error stopped new lease');

    await fetch('/test/delay-arm-failure', {method: 'POST'});
    $('arm-state').click();
    await until(async () => await count('arm/state') === 2, 'previous-session arm read in flight');
    $('release').click();
    await until(() => !$('acquire').disabled, 'released before new session');
    await ready();
    const stopsBeforePreviousFailure = await count('dog/stop');
    await delay(900);
    assert($('owner-state').textContent.includes('本页持有'), 'previous-session arm error revoked new lease');
    assert(await count('dog/stop') === stopsBeforePreviousFailure, 'previous-session arm error stopped new lease');
    $('release').click();
    result = {ok: true};
  } catch (error) { result = {ok: false, error: error.stack || String(error)}; }
  await fetch('/test/result', {method: 'POST', body: JSON.stringify(result)});
})();
</script>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser')
    args = parser.parse_args()
    browser_path = args.browser or find_browser()
    page = (Path(__file__).resolve().parents[1] / 'src/web/control.html').read_text(encoding='utf-8')
    page = page.replace('</body>', OBSERVER + '</body>').encode()
    frame = (Path(__file__).parent / 'fixtures/camera-frame.jpg').read_bytes()
    observations = queue.Queue()
    calls = []
    lock = threading.Lock()
    fixture = {'lease': None, 'phase': 'disarmed', 'fail': False, 'delay_acquire': False,
               'camera': True, 'dog_error': None, 'delay_status': False, 'delay_arm_failure': False}

    def status():
        return {'mode': 'simulation', 'estop': False, 'safety_revision': 'fixture-current-latch', 'lease_active': fixture['lease'] is not None,
                'dog': {'phase': fixture['phase'], 'velocity': {'vx': 0, 'vy': 0, 'yaw': 0},
                        'limits': {'vx': 0.2, 'vy': 0.1, 'yaw': 0.3}, 'error': fixture['dog_error'], 'stop_verified': False},
                'arm': {'motion_enabled': True, 'reason': 'simulation', 'simulated': True,
                        'position_deg': {'b': 0, 's': 0, 'e': 0, 'w': 0, 'h': 0}}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, value, content_type='application/json'):
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
            try:
                self.send_response(code)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.path == '/':
                self.reply(200, page, 'text/html; charset=utf-8')
            elif self.path == '/api/status':
                value = status()
                if fixture['delay_status']:
                    fixture['delay_status'] = False
                    with lock:
                        calls.append({'action': 'delayed-status', 'body': {}})
                    time.sleep(0.6)
                self.reply(200, value)
            elif self.path == '/test/calls':
                with lock:
                    self.reply(200, list(calls))
            elif self.path.startswith('/snapshot.jpg'):
                self.reply(200 if fixture['camera'] else 503, frame if fixture['camera'] else {}, 'image/jpeg')
            else:
                self.reply(404, {})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
            if self.path.startswith('/test/'):
                if self.path == '/test/result':
                    observations.put(body)
                elif self.path == '/test/fail-next':
                    fixture['fail'] = True
                elif self.path == '/test/delay-acquire':
                    fixture['delay_acquire'] = True
                elif self.path == '/test/camera-offline':
                    fixture['camera'] = False
                elif self.path == '/test/dog-error':
                    fixture['dog_error'] = 'UDP transport failed'
                    fixture['phase'] = 'error'
                elif self.path == '/test/delay-status':
                    fixture['delay_status'] = True
                elif self.path == '/test/delay-arm-failure':
                    fixture['delay_arm_failure'] = True
                self.reply(200, {})
                return
            if self.headers.get('Authorization') != 'Bearer browser-test-secret':
                self.reply(401, {'error': 'wrong token'})
                return
            action = self.path[len('/api/'):]
            with lock:
                calls.append({'action': action, 'body': body})
                velocity_count = sum(row['action'] == 'dog/velocity' for row in calls)
            if action == 'control/acquire':
                if fixture['delay_acquire']:
                    fixture['delay_acquire'] = False
                    time.sleep(0.5)
                fixture['lease'] = 'browser-lease-' + str(len(calls))
                self.reply(200, {'status': status(), 'lease': fixture['lease']})
                return
            if action == 'dog/stand':
                fixture['phase'] = 'ready'
            elif action == 'arm/state' and fixture['delay_arm_failure']:
                fixture['delay_arm_failure'] = False
                time.sleep(0.6)
                self.reply(503, {'error': 'old arm read failed'})
                return
            elif action == 'dog/velocity':
                if fixture['fail']:
                    fixture['fail'] = False
                    self.reply(409, {'error': 'injected motion failure'})
                    return
                if velocity_count == 1:
                    time.sleep(0.5)
            elif action in ('dog/stop', 'control/release', 'control/estop'):
                fixture['lease'] = None
                fixture['phase'] = 'disarmed'
            elif action == 'control/reset':
                if body.get('revision') != 'fixture-current-latch':
                    self.reply(409, {'error': 'Stale safety revision'})
                    return
                fixture['dog_error'] = None
                fixture['phase'] = 'disarmed'
            self.reply(200, status())

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server, tempfile.TemporaryDirectory(prefix='lite3-dev-chrome-') as profile:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        browser = subprocess.Popen([browser_path, '--headless', '--disable-gpu', '--disable-background-networking',
                                    '--disable-extensions', '--no-first-run', '--user-data-dir=' + profile,
                                    'http://127.0.0.1:{}/'.format(server.server_port)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            result = observations.get(timeout=55)
        finally:
            if os.name == 'nt' and browser.poll() is None:
                subprocess.run(['taskkill', '/PID', str(browser.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            elif browser.poll() is None:
                browser.terminate()
            try:
                browser.wait(timeout=5)
            except subprocess.TimeoutExpired:
                browser.kill()
                browser.wait()
            server.shutdown()
    assert result['ok'], result.get('error', 'Browser checks failed')
    print('PASS: real Chrome hold/release, blur, delayed replies, HTTP failure, explicit reacquire, stale camera clearing.')


if __name__ == '__main__':
    main()
