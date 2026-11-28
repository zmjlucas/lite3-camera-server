"""Read-only host diagnostics, also runnable over SSH before deployment."""
import glob
import http.client
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
import types

if __package__:
    from .. import camera_sources, runtime_info


SERVICE = 'lite3-dev-server.service'
DEFAULT_ARM = '/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0'
MODULE_PROBE = "import importlib,sys; m=importlib.import_module(sys.argv[1]); print(getattr(m,'__version__','import OK'))"


def run(argv):
    try:
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True, timeout=10)
        return {'state': 'ok' if result.returncode == 0 else 'failed',
                'output': result.stdout[:16000].strip(), 'exit': result.returncode}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {'state': 'unverified', 'output': str(error)}


def check(label, argv):
    print('\n' + label, flush=True)
    result = run(argv)
    print(json.dumps(result, indent=2), flush=True)


def service_status():
    properties = ('LoadState', 'ActiveState', 'SubState', 'MainPID', 'User', 'Group',
                  'SupplementaryGroups', 'ExecStart', 'FragmentPath')
    result = run(['systemctl', 'show', '--no-pager', '--property=' + ','.join(properties), SERVICE])
    result['properties'] = dict(line.split('=', 1) for line in result['output'].splitlines() if '=' in line)
    return result


def fetch_diagnostics(port):
    """A bounded loopback OPTIONS read; legacy handlers reject it without state IO."""
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
    try:
        connection.request('OPTIONS', '/api/diagnostics')
        response = connection.getresponse()
        if response.status != 200 or response.getheader('X-Lite3-Dev-Protocol') != '1':
            raise ValueError('HTTP {}: compatible passive diagnostics unavailable'.format(response.status))
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError('Diagnostic response exceeds 1 MiB')
        value = json.loads(body.decode('utf-8'))
        if (not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 1
                or not isinstance(value.get('control'), dict)
                or not isinstance(value.get('camera'), dict)
                or not isinstance(value.get('runtime'), (dict, type(None)))):
            raise ValueError('Unsupported diagnostic snapshot')
        control = value['control']
        if (control.get('mode') not in ('hardware', 'readonly', 'disabled')
                or any(not isinstance(control.get(key), dict) for key in ('dog', 'arm'))
                or not isinstance(control['dog'].get('feedback'), dict)):
            raise ValueError('Invalid control snapshot')
        runtime = value.get('runtime') or {}
        if any(not isinstance(runtime.get(key, {}), dict) for key in ('version', 'config')):
            raise ValueError('Invalid runtime metadata')
        if any(runtime.get(key) is not None and (not isinstance(runtime[key], str) or '\0' in runtime[key])
               for key in ('directory', 'python')):
            raise ValueError('Invalid runtime paths')
        identity = runtime.get('identity')
        if identity is not None and (not isinstance(identity, dict)
                or any(type(identity.get(key)) is not int for key in ('uid', 'gid'))
                or not isinstance(identity.get('groups'), list)
                or any(type(group) is not int for group in identity['groups'])):
            raise ValueError('Invalid runtime identity')
        config = runtime.get('config', {})
        if any(config.get(key) is not None and (not isinstance(config[key], str) or '\0' in config[key])
               for key in ('dog_host', 'arm_device', 'arm_host')):
            raise ValueError('Invalid runtime device configuration')
        port = config.get('dog_state_port')
        if port is not None and (type(port) is not int or not 1 <= port <= 65535):
            raise ValueError('Invalid runtime UDP port')
        return {'state': 'observed', 'snapshot': value}
    except (OSError, http.client.HTTPException, ValueError, RecursionError) as error:
        return {'state': 'unverified', 'error': str(error)}
    finally:
        connection.close()


def feedback_report(control):
    if not isinstance(control, dict):
        return {'state': 'unverified', 'reason': 'No compatible running service snapshot'}
    dog = control.get('dog') if isinstance(control.get('dog'), dict) else {}
    arm = control.get('arm') if isinstance(control.get('arm'), dict) else {}
    if (control.get('mode') in ('disabled', 'readonly') or dog.get('simulated') is True
            or arm.get('capabilities')):
        return {'state': 'not_applicable', 'reason': 'This control mode has no hardware dog receiver'}
    feedback = dog.get('feedback') if isinstance(dog.get('feedback'), dict) else {}
    reason = feedback.get('reason') if isinstance(feedback.get('reason'), str) else ''
    state = 'unverified'
    if feedback.get('observed') is True and type(feedback.get('fresh')) is bool:
        state = 'feedback_fresh' if feedback['fresh'] else 'feedback_stale'
    elif 'could not bind UDP port' in reason:
        state = 'port_conflict' if '[Errno 98]' in reason or 'Address already in use' in reason else 'bind_failed'
    elif reason == 'Waiting for dog state feedback':
        state = 'feedback_missing'
    elif reason == 'Invalid dog state feedback received':
        state = 'invalid_feedback'
    return {'state': state, 'feedback': feedback}


def service_identity(service, runtime, override=None):
    if override is None and runtime.get('identity'):
        return {'state': 'observed', 'source': 'running process', 'account': runtime['identity']}
    properties = service.get('properties', {})
    if override is None and properties.get('LoadState') != 'loaded':
        return {'state': 'unverified', 'reason': 'Service user unavailable; use --service-user for a planned installation'}
    try:
        import pwd
        import grp
        name = override if override is not None else properties.get('User') or '0'
        account = pwd.getpwuid(int(name)) if name.isdigit() else pwd.getpwnam(name)
        group = properties.get('Group') if override is None else None
        gid = (int(group) if group.isdigit() else grp.getgrnam(group).gr_gid) if group else account.pw_gid
        groups = set(os.getgrouplist(account.pw_name, gid))
        if override is None:
            for group in properties.get('SupplementaryGroups', '').split():
                groups.add(int(group) if group.isdigit() else grp.getgrnam(group).gr_gid)
        return {'state': 'configured', 'source': 'override' if override is not None else 'systemd unit',
                'account': {'name': account.pw_name, 'uid': account.pw_uid, 'gid': gid, 'groups': sorted(groups)}}
    except (ImportError, KeyError, ValueError, OSError, AttributeError) as error:
        return {'state': 'unverified', 'reason': 'Cannot resolve service identity: ' + str(error)}


def _mode_allows(info, account, bits):
    if account['uid'] == 0:
        return True
    shift = 6 if account['uid'] == info.st_uid else (
        3 if info.st_gid in set(account['groups'] + [account['gid']]) else 0)
    return ((info.st_mode >> shift) & bits) == bits


def inspect_device(path, account):
    """Stat only: opening a serial node can toggle reset lines."""
    resolved = os.path.realpath(path)
    result = {'path': path, 'resolved': resolved, 'mode_access': 'unverified',
              'scope': 'Unix mode bits; ACLs, device policies and runtime access remain unverified'}
    try:
        info = os.stat(path)
        result.update(state='observed', mode=stat.filemode(info.st_mode), uid=info.st_uid,
                      gid=info.st_gid, character_device=stat.S_ISCHR(info.st_mode))
        if account:
            # Check both the requested alias path and its resolved target's parents.
            parents = set(Path(path).absolute().parents) | set(Path(resolved).parents)
            traversable = all(_mode_allows(parent.stat(), account, 1) for parent in parents)
            result['mode_access'] = 'read_write' if traversable and _mode_allows(info, account, 6) else 'denied'
    except FileNotFoundError:
        result['state'] = 'missing'
    except OSError as error:
        result.update(state='unverified', error=str(error))
    return result


def device_owners(devices, proc_root=Path('/proc')):
    """Read visible descriptors without opening, locking or signaling a device."""
    owners = {os.path.realpath(path): [] for path in devices}
    result = {'scope': 'visible_processes_only', 'devices': owners, 'unreadable_processes': 0}
    try:
        processes = list(Path(proc_root).iterdir())
    except OSError as error:
        result['error'] = str(error)
        return result
    for process in processes:
        if not process.name.isdigit():
            continue
        try:
            descriptors = list((process / 'fd').iterdir())
        except OSError:
            result['unreadable_processes'] += 1
            continue
        matches = set()
        for descriptor in descriptors:
            try:
                target = os.readlink(str(descriptor))
                if target in owners:
                    matches.add(target)
            except OSError:
                continue
        if matches:
            try:
                name = (process / 'comm').read_text(errors='replace').strip()
            except OSError:
                name = 'unknown'
            for target in matches:
                owners[target].append({'pid': int(process.name), 'name': name})
    return result


def collect_report(options, module_probe=MODULE_PROBE):
    service = service_status()
    running = fetch_diagnostics(options.get('remote_port') or 8080)
    snapshot = running.get('snapshot', {})
    runtime = snapshot.get('runtime') or {}
    configuration = dict(runtime.get('config') or {})
    defaults = {'dog_host': '192.168.1.120', 'dog_state_port': 43897, 'arm_device': DEFAULT_ARM}
    for name, default in defaults.items():
        configuration[name] = options.get(name) or configuration.get(name) or default
    identity = service_identity(service, runtime, options.get('service_user'))
    account = identity.get('account')
    directory = options.get('install_dir') or str(Path.home() / 'lite3-dev-server')
    version = runtime_info.bundle_version(directory)
    installed = {'directory': directory, 'version': version,
                 'state': 'observed' if version['sha256'] else 'incomplete'}
    try:
        (Path(directory) / 'server.py').stat()
    except FileNotFoundError:
        installed['state'] = 'not_deployed'
    except OSError as error:
        installed.update(state='unverified', error=str(error))
    running_hash = (runtime.get('version') or {}).get('sha256')
    installed['matches_running'] = (running_hash == version['sha256']) if running_hash and version['sha256'] else None
    installed['hint'] = 'Fingerprints cover the deployment bundle. A mismatch means installed files differ from the startup snapshot; plan an explicit restart to load them.'

    try:
        sources = camera_sources.discover_sources()
        cameras = {'state': 'observed' if sources else 'not_detected', 'sources': sources}
    except (OSError, RuntimeError, ImportError) as error:
        sources = []
        cameras = {'state': 'unverified', 'error': str(error), 'sources': []}
    cameras['health'] = snapshot.get('camera')
    cameras['nodes'] = glob.glob('/dev/video*') + glob.glob('/dev/v4l/by-id/*')
    cameras['scope'] = 'Discovery runs as the SSH account; service-user mode bits are evaluated separately'
    cameras['permissions'] = [inspect_device(source['device'], account) for source in sources]
    cameras['hint'] = 'Confirm RGB nodes by camera label and formats. Keep the running service capture; inspect competing PIDs before changing ownership.'
    local_arm = not configuration.get('arm_host') and not configuration.get('simulate')
    serial = {'state': 'inspected' if local_arm else 'not_applicable',
              'hint': 'Check the resolved node and its group. Configure service SupplementaryGroups (commonly dialout), pySerial and device policy; inspect competing PIDs. Serial I/O and reset lines were left untouched.'}
    if local_arm:
        serial['device'] = inspect_device(configuration['arm_device'], account)
    else:
        serial['reason'] = 'Serial is simulated or belongs to the configured remote arm host'
    devices = [device for source in sources for device in source.get('related_devices', [source['device']])]
    if local_arm:
        devices.append(configuration['arm_device'])
    ownership = device_owners(devices)
    for source in sources:
        owners = [owner for device in source.get('related_devices', [source['device']])
                  for owner in ownership['devices'].get(os.path.realpath(device), [])]
        source['owners'] = owners
        if owners and all(owner['pid'] == runtime.get('pid') for owner in owners):
            source['occupancy'] = 'running_service'
            if (source.get('error') or '').startswith('Camera is in use by '):
                source['error'] = 'Owned by the running service; existing capture is preserved.'
        else:
            source['occupancy'] = 'other_process' if owners else 'unverified'
            if owners and (source.get('error') or '').startswith('Camera is in use by '):
                source['error'] = 'Camera has competing visible owners; inspect all listed PIDs before changing capture.'
    ownership['hint'] = 'An empty owner list cannot establish exclusive availability; procfs permissions can hide processes.'
    udp = feedback_report(snapshot.get('control'))
    receiver = runtime.get('config') or {}
    udp.update(host=receiver.get('dog_host'), port=receiver.get('dog_state_port'),
               configuration_source='running service' if receiver else 'unverified')
    udp['inspection'] = {'host': configuration['dog_host'], 'port': configuration['dog_state_port'],
                         'configuration_source': 'running service with explicit overrides' if receiver else 'defaults with explicit overrides'}
    udp['inspection']['listeners'] = run(['ss', '-H', '-lunp', 'sport = :{}'.format(configuration['dog_state_port'])])
    udp['hint'] = ('Verify motion-host upload destination is this server and UDP port {port}, source IP is {host}, '
                   'and routes/firewalls allow uploads. Accepted posture is a 220-byte 0x0901 packet (208-byte RobotState). '
                   'An existing listener alone cannot prove feedback arrival. For a bind conflict, inspect its PID or plan '
                   'a separate port and matching upload destination. Doctor never binds or sends UDP.').format(**udp['inspection'])
    python = sys.executable
    modules = {name: run([python, '-B', '-c', module_probe, name])
               for name in ('numpy', 'cv2', 'pyrealsense2', 'serial')}
    dependencies = {'python': python, 'modules': modules,
                    'runtime_python': runtime.get('python'),
                    'scope': 'Imports use the doctor interpreter as the SSH user; service interpreter/environment and remote arm dependencies remain unverified'}
    return {'service': service, 'installed': installed, 'running': running, 'identity': identity,
            'camera': cameras, 'udp': udp, 'serial': serial, 'ownership': ownership, 'dependencies': dependencies}


def render_report(report):
    return '\n\n'.join('{}\n{}'.format(name.upper(), json.dumps(value, indent=2, sort_keys=True))
                       for name, value in report.items())


def main():
    global camera_sources, runtime_info
    ros_diagnostic, module_probe, payload = sys.argv[1:]
    payload = json.loads(payload)
    # Trusted launcher sources travel in memory; no deployment or temporary files.
    for name in ('build_info', 'runtime_info', 'wide_angle', 'camera_sources'):
        module = types.ModuleType(name)
        module.__file__ = '<doctor/' + name + '.py>'
        sys.modules[name] = module
        exec(compile(payload['modules'][name], module.__file__, 'exec'), module.__dict__)
    runtime_info = sys.modules['runtime_info']
    camera_sources = sys.modules['camera_sources']
    print('Host: ' + platform.platform(), flush=True)
    print('Doctor Python: ' + sys.version.split()[0], flush=True)
    print(render_report(collect_report(payload['options'], module_probe)), flush=True)
    check('OS', ['cat', '/etc/os-release'])
    check('Camera service status', ['systemctl', 'is-active', 'realsense', 'realsense_ros2'])
    setups = sorted(glob.glob('/opt/ros/*/setup.bash'))
    print('ROS setup files: ' + json.dumps(setups), flush=True)
    for setup in setups:
        for mode, label in (('environment', 'ROS environment'), ('package', 'web_video_server'),
                            ('topics', 'ROS topics (check the image topic name)')):
            check(label + ': ' + setup,
                  ['bash', '-lc', ros_diagnostic, 'lite3-camera-doctor', setup, mode])


if __name__ == '__main__':
    main()
