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
  const joint = name => document.querySelector(`[data-joint="${name}"][data-delta="1"]`);
  const visible = element => element.getClientRects().length > 0;
  const armEnvironment = scenario => fetch('/test/arm-environment', {method: 'POST', body: JSON.stringify({scenario})});
  async function ready() {
    $('acquire').click();
    await until(() => !$('stand').disabled, 'acquired');
    if ($('environment').textContent.includes('dist'))
      assert(!$('notice').textContent.includes('模拟'), 'distribution acquisition describes real dog control');
    $('stand').click();
    await until(() => !document.querySelector('[data-axis="vx"]').disabled, 'stand ready');
  }
  const press = () => document.querySelector('[data-axis="vx"]').dispatchEvent(
    new PointerEvent('pointerdown', {bubbles: true, pointerId: 1, button: 0}));
  const release = () => window.dispatchEvent(new PointerEvent('pointerup', {pointerId: 1}));
  let result;
  try {
    await until(() => $('connection').textContent.includes('在线'), 'status');
    assert($('environment')?.textContent.includes('dist'), 'distribution environment must be labeled separately');
    assert($('mode').textContent.includes('硬件'), 'distribution hardware mode must be shown');
    assert(!visible(joint('e')) && !visible($('arm-stop')) && !visible($('arm-operation')),
      'distribution must hide arm diagnostics');
    assert(!$('acquire').disabled, 'control must be available when the server is online');
    assert($('dog-action-start')?.disabled, 'predefined actions require control and standing preparation');
    await ready();
    press();
    await until(async () => await count('dog/velocity') === 1, 'first velocity');
    assert($('dog-action-start').disabled, 'held drive blocks timed action');
    await owned('dogAction', {name: 'forward', speed: 0.1, duration: 1});
    assert(await count('dog/action') === 0, 'held drive rejects competing owned actions');
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
    await until(() => !$('acquire').disabled, 'released before distribution feedback');
    $('arm-state').click();
    await until(() => $('joint-e').textContent === '12.5', 'distribution hardware feedback renders without a lease');
    assert($('joint-h').textContent === '未知', 'distribution leaves the unobserved gripper unknown');
    await delay(900);
    assert($('joint-e').textContent === '12.5', 'ordinary hardware status polls preserve the last explicit read');

    await armEnvironment('commissioned');
    await until(() => $('environment').textContent.includes('dev'), 'development hardware environment');
    assert($('mode').textContent.includes('硬件') && $('arm-mode').textContent.includes('诊断'),
      'commissioned hardware keeps hardware mode and exposes its diagnostic state');
    assert(visible(joint('e')) && visible($('arm-stop')) && visible($('arm-operation')),
      'commissioned development must expose arm diagnostics');
    $('acquire').click();
    await until(() => !joint('h').disabled, 'commissioned gripper available');
    assert(joint('s').disabled && $('stand').disabled, 'commissioned arm restricts joints and disables dog output');
    assert($('notice').textContent.includes('诊断') && !$('notice').textContent.includes('站立准备'),
      'commissioned acquisition directs the operator to arm diagnostics');
    joint('h').click();
    await until(async () => await count('arm/jog') === 1, 'commissioned jog');
    const commissionedJog = (await calls()).find(row => row.action === 'arm/jog').body;
    assert(commissionedJog.delta === 0.5 && commissionedJog.speed === 1, 'commissioned gripper bounds');
    $('release').click();
    await until(() => !$('acquire').disabled, 'released before simulation');

    await armEnvironment('simulated');
    await until(() => $('arm-mode').textContent.includes('模拟'), 'simulation indicated by arm status');
    assert($('environment').textContent.includes('dev') && $('mode').textContent.includes('硬件'),
      'simulation must keep the separate development and hardware labels');
    await ready();
    assert(!joint('s').disabled && $('joint-h').textContent === '0.0', 'simulated shoulder and gripper targets available');
    joint('h').click();
    await until(async () => await count('arm/jog') === 2, 'simulated jog');
    const simulatedJog = (await calls()).filter(row => row.action === 'arm/jog')[1].body;
    assert(simulatedJog.delta === 1 && simulatedJog.speed === 2, 'simulation uses its own jog limits');
    $('release').click();
    await until(() => !$('acquire').disabled, 'released before distribution restart');

    await armEnvironment('distribution');
    await until(() => $('environment').textContent.includes('dist'), 'distribution restart');
    assert($('joint-h').textContent === '未知' && $('joint-e').textContent === '未知' &&
      !$('arm-caption').textContent.includes('模拟'), 'distribution restart discards simulated targets');

    await ready();
    await until(() => !$('dog-action-start').disabled, 'feedback enables predefined action');
    assert($('dog-feedback').textContent.includes('实测') && $('dog-feedback').textContent.includes('6 / 0 / 0'),
      'measured standing feedback must be visible separately from command phase');
    $('dog-action-speed').value = '0.21';
    $('dog-action-start').click();
    await delay(100);
    assert(await count('dog/action') === 0, 'invalid action speed must not be submitted');
    $('dog-action-speed').value = '0.1';
    $('dog-action-duration').value = '11';
    $('dog-action-start').click();
    await delay(100);
    assert(await count('dog/action') === 0, 'invalid action duration must not be submitted');
    $('dog-action-duration').value = '1';
    $('dog-action-start').click();
    await until(async () => await count('dog/action') === 1 && $('dog-action-status').textContent.includes('执行中'), 'forward starts');
    const firstAction = (await calls()).find(row => row.action === 'dog/action').body;
    assert(firstAction.name === 'forward' && firstAction.speed === 0.1 && firstAction.duration === 1 &&
      firstAction.seq === 2 && typeof firstAction.lease === 'string', 'action uses numeric defaults and shared lease sequence');
    assert(document.querySelector('[data-axis="vx"]').disabled && $('dog-action-start').disabled,
      'running action disables manual drive and repeat start');
    const heartbeats = await count('control/heartbeat');
    await until(async () => await count('control/heartbeat') > heartbeats, 'action retains explicit heartbeat');
    const actionStops = await count('dog/stop'), actionAcquisitions = await count('control/acquire');
    await fetch('/test/complete-action', {method: 'POST'});
    await until(() => $('dog-action-status').textContent.includes('已完成') && !$('acquire').disabled, 'completion releases ownership');
    assert($('notice').textContent.includes('完成') && !$('notice').textContent.includes('失败'),
      'natural completion must retain a success notice after lease revocation');
    await delay(900);
    assert(await count('dog/stop') === actionStops && await count('control/acquire') === actionAcquisitions,
      'completion must avoid a redundant stop or automatic reacquisition');

    await ready();
    $('dog-action-start').click();
    await until(() => $('dog-action-status').textContent.includes('执行中'), 'action before ownership handoff');
    const handoffStops = await count('dog/stop'), handoffAcquisitions = await count('control/acquire');
    await fetch('/test/complete-action', {method: 'POST', body: JSON.stringify({next_owner: true})});
    await until(() => $('release').disabled, 'completed action clears local ownership after another controller acquires');
    assert($('notice').textContent.includes('完成') && $('owner-state').textContent.includes('已占用'),
      'completion must preserve the result when another controller already holds the server lease');
    await delay(900);
    assert(await count('dog/stop') === handoffStops && await count('control/acquire') === handoffAcquisitions,
      'completed local action must not stop or reacquire another controller lease');
    await fetch('/test/release-other-controller', {method: 'POST'});
    await until(() => !$('acquire').disabled, 'other controller releases');

    await ready();
    await fetch('/test/delay-action', {method: 'POST', body: JSON.stringify({scenario: 'completed'})});
    $('dog-action-duration').value = '0.1';
    const shortActionStops = await count('dog/stop');
    $('dog-action-start').click();
    await until(() => !$('acquire').disabled, 'short action releases control before delayed start reply');
    assert($('notice').textContent.includes('完成') && !$('notice').textContent.includes('失败'),
      'short action completion must survive a delayed start response and rejected heartbeat');
    await delay(900);
    assert($('dog-action-status').textContent.includes('已完成') && await count('dog/stop') === shortActionStops,
      'late action start response must preserve completion without a redundant stop');

    await ready();
    await fetch('/test/delay-action', {method: 'POST', body: JSON.stringify({scenario: 'rejected'})});
    $('dog-action-start').click();
    await until(() => !$('acquire').disabled, 'rejected start releases local control');
    assert($('notice').textContent.includes('失败'), 'an old completed action must not hide a rejected new start');
    $('dog-action-duration').value = '1';

    await ready();
    await fetch('/test/dog-feedback', {method: 'POST', body: JSON.stringify({scenario: 'stale'})});
    await until(() => $('dog-action-start').disabled && $('dog-feedback').textContent.includes('过期'), 'stale feedback blocks action');
    assert($('dog-feedback').textContent.includes('fixture stale feedback'), 'feedback reason remains visible');
    await fetch('/test/dog-feedback', {method: 'POST', body: JSON.stringify({scenario: 'simulated'})});
    await until(() => !$('dog-action-start').disabled && $('dog-feedback').textContent.includes('模拟'), 'simulated feedback is labeled');
    await fetch('/test/dog-feedback', {method: 'POST', body: JSON.stringify({scenario: 'measured'})});
    await until(() => $('dog-feedback').textContent.includes('实测'), 'measured feedback restored');
    const cancellations = [
      ['turn_left', 0.2, () => window.dispatchEvent(new Event('blur'))],
      ['backward', 0.1, () => {
        Object.defineProperty(document, 'hidden', {configurable: true, value: true});
        document.dispatchEvent(new Event('visibilitychange'));
        delete document.hidden;
        document.dispatchEvent(new Event('visibilitychange'));
      }],
      ['turn_right', 0.2, () => $('dog-stop').click()]
    ];
    for (const [name, speed, cancel] of cancellations) {
      if ($('stand').disabled && !$('owner-state').textContent.includes('本页持有')) await ready();
      $('dog-action-name').value = name;
      $('dog-action-name').dispatchEvent(new Event('change'));
      const previous = await count('dog/action');
      $('dog-action-start').click();
      await until(async () => await count('dog/action') === previous + 1 && $('dog-action-status').textContent.includes('执行中'), name + ' starts');
      const request = (await calls()).filter(row => row.action === 'dog/action').pop().body;
      assert(request.name === name && request.speed === speed, 'action selection updates direction and default speed');
      const stops = await count('dog/stop');
      cancel();
      await until(async () => await count('dog/stop') === stops + 1 && !$('acquire').disabled, name + ' cancelled');
      assert($('dog-action-status').textContent.includes('已取消'), 'cancellation result remains visible');
    }

    await ready();
    await armEnvironment('legacy');
    await until(() => !$('environment').textContent.includes('dist'), 'legacy status replaces distribution status');
    assert($('notice').textContent.includes('重启') && $('acquire').disabled &&
      document.querySelector('[data-axis="vx"]').disabled, 'unknown environment blocks acquisition and motion');
    assert(!$('dog-stop').disabled && !$('estop').disabled, 'legacy status keeps stop actions available');
    const legacyStops = await count('dog/stop');
    $('dog-stop').click();
    await until(async () => await count('dog/stop') === legacyStops + 1, 'operator can stop during legacy status');
    assert($('acquire').disabled, 'unknown environment stays blocked after releasing the lease');
    await armEnvironment('distribution');
    await until(() => !$('acquire').disabled, 'released before status failure');
    await fetch('/test/status-offline', {method: 'POST'});
    await until(() => $('connection').textContent.includes('离线'), 'status unavailable');
    const observerStops = await count('dog/stop');
    $('dog-stop').click();
    await until(async () => await count('dog/stop') === observerStops + 1, 'observer stop during status failure');
    await until(() => $('connection').textContent.includes('离线'), 'status still unavailable');
    const emergencyStops = await count('control/estop');
    $('estop').click();
    await until(async () => await count('control/estop') === emergencyStops + 1, 'emergency stop during status failure');
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
               'camera': True, 'dog_error': None, 'delay_status': False, 'delay_arm_failure': False,
               'status_offline': False, 'arm_environment': 'distribution',
               'dog_action': None, 'dog_feedback': 'measured', 'completion_heartbeat': False,
               'delay_action': None}

    def status():
        simulated = fixture['arm_environment'] == 'simulated'
        commissioned = fixture['arm_environment'] == 'commissioned'
        feedback = {'observed': True, 'simulated': fixture['dog_feedback'] == 'simulated',
                    'fresh': fixture['dog_feedback'] != 'stale',
                    'age_s': 1.5 if fixture['dog_feedback'] == 'stale' else 0.05,
                    'basic_state': 6, 'gait_state': 0, 'motion_state': 0,
                    'standing': True, 'motion_ready': fixture['dog_feedback'] != 'stale',
                    'reason': 'fixture stale feedback' if fixture['dog_feedback'] == 'stale' else None}
        arm = {'motion_enabled': simulated or commissioned,
               'reason': 'Fixture arm state', 'simulated': simulated,
               'position_deg': dict.fromkeys('bsewh', 0) if simulated else None}
        if commissioned:
            arm.update(operation=None, capabilities={
                'joints': ['b', 'e', 'h'], 'max_delta_deg': {'b': .5, 'e': 2, 'h': .5},
                'max_speed_deg_s': 1, 'max_travel_deg': 3, 'asynchronous': True,
                'stop_verified': False, 'gripper_feedback': False,
            })
        result = {'mode': 'hardware', 'estop': False, 'safety_revision': 'fixture-current-latch',
                  'lease_active': fixture['lease'] is not None,
                  'dog': {'phase': fixture['phase'], 'velocity': {'vx': 0, 'vy': 0, 'yaw': 0},
                          'limits': {'vx': 0.2, 'vy': 0.1, 'yaw': 0.3}, 'error': fixture['dog_error'],
                          'stop_verified': False, 'simulated': simulated, 'motion_enabled': not commissioned,
                          'feedback': feedback, 'action': fixture['dog_action'],
                          'actions_enabled': not commissioned and fixture['phase'] == 'ready' and feedback['motion_ready']},
                  'arm': arm}
        if fixture['arm_environment'] != 'legacy':
            result['environment'] = 'dev' if simulated or commissioned else 'dist'
        return result

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, value, content_type='application/json'):
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
            try:
                self.send_response(code)
                self.send_header('Content-Type', content_type)
                self.send_header('X-Lite3-Dev-Protocol', '1')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.path == '/':
                self.reply(200, page, 'text/html; charset=utf-8')
            elif self.path == '/static/dev_client.js':
                script = (Path(__file__).resolve().parents[1] / 'src/web/dev_client.js').read_bytes()
                self.reply(200, script, 'application/javascript; charset=utf-8')
            elif self.path == '/api/status':
                if fixture['completion_heartbeat']:
                    time.sleep(0.7)
                if fixture['status_offline']:
                    self.reply(503, {'error': 'status unavailable'})
                    return
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
                elif self.path == '/test/status-offline':
                    fixture['status_offline'] = True
                elif self.path == '/test/arm-environment':
                    fixture['arm_environment'] = body['scenario']
                elif self.path == '/test/complete-action':
                    fixture['dog_action'] = dict(fixture['dog_action'], phase='completed', remaining_s=0)
                    fixture['lease'] = 'other-controller' if body.get('next_owner') else None
                    fixture['phase'] = 'disarmed'
                    fixture['completion_heartbeat'] = True
                elif self.path == '/test/release-other-controller':
                    fixture['lease'] = None
                elif self.path == '/test/dog-feedback':
                    fixture['dog_feedback'] = body['scenario']
                elif self.path == '/test/delay-action':
                    fixture['delay_action'] = body['scenario']
                self.reply(200, {})
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
            elif action == 'dog/action':
                delayed = fixture['delay_action']
                fixture['delay_action'] = None
                if delayed == 'rejected':
                    fixture['lease'] = None
                    fixture['phase'] = 'disarmed'
                    fixture['completion_heartbeat'] = True
                    time.sleep(0.7)
                    self.reply(409, {'error': 'New action rejected'})
                    return
                fixture['dog_action'] = dict(id='action-' + str(len(calls)), name=body['name'],
                                             speed=body['speed'], duration=body['duration'],
                                             phase='running', reason=None, remaining_s=body['duration'])
                fixture['phase'] = 'moving'
                if delayed == 'completed':
                    value = status()
                    time.sleep(body['duration'])
                    fixture['dog_action'] = dict(fixture['dog_action'], phase='completed', remaining_s=0)
                    fixture['lease'] = None
                    fixture['phase'] = 'disarmed'
                    fixture['completion_heartbeat'] = True
                    time.sleep(0.9)
                    self.reply(200, value)
                    return
            elif action == 'control/heartbeat' and fixture['completion_heartbeat']:
                fixture['completion_heartbeat'] = False
                self.reply(409, {'error': 'Lease expired after action completion'})
                return
            elif action == 'arm/state' and fixture['delay_arm_failure']:
                fixture['delay_arm_failure'] = False
                time.sleep(0.6)
                self.reply(503, {'error': 'old arm read failed'})
                return
            elif action == 'arm/state':
                arm = status()['arm']
                arm.update(observed=True, position_deg={'b': 0, 's': 0, 'e': 12.5, 'w': 0}, gripper_position_deg=None)
                self.reply(200, arm)
                return
            elif action == 'dog/velocity':
                if fixture['fail']:
                    fixture['fail'] = False
                    self.reply(409, {'error': 'injected motion failure'})
                    return
                if velocity_count == 1:
                    time.sleep(0.5)
            elif action in ('dog/stop', 'control/release', 'control/estop'):
                if fixture['dog_action'] and fixture['dog_action']['phase'] == 'running':
                    fixture['dog_action'] = dict(fixture['dog_action'], phase='cancelled', remaining_s=0)
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
    print('PASS: Chrome control lifecycle, dog actions and feedback, stale camera clearing, dev diagnostics, and simulation.')


if __name__ == '__main__':
    main()
