"""Create or check R5 profiles using local Linux files and device metadata only.

Run as the intended service user on the arm host. Independently confirm all five
targets, four measured references and the gripper reference in the same controller
session before filling the template. Serial preflight still runs before motion.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.arm_motion import TARGET_LIMITS, _Profile, _identity, _trusted
from src.remote.arm_worker import LIMITS_DEG, validate_device


def _access(path, mode, **kwargs):
    if os.access in os.supports_effective_ids:
        kwargs['effective_ids'] = True
    return os.access(path, mode, **kwargs)


def _device_metadata(device):
    validate_device(device)
    info = os.stat(device)
    if not stat.S_ISCHR(info.st_mode):
        raise ValueError('Arm device must be a character device.')
    readable, writable = _access(device, os.R_OK), _access(device, os.W_OK)
    if not readable or not writable:
        raise ValueError('Service user needs read and write permission on the arm device.')
    return {'path': device, 'resolved_path': os.path.realpath(device),
            'readable': readable, 'writable': writable,
            'uid': info.st_uid, 'gid': info.st_gid, 'mode': oct(stat.S_IMODE(info.st_mode))}


def template(path, device):
    metadata = _device_metadata(device)
    machine, boot = _identity()
    record = {'version': 1, 'machine_id': machine, 'boot_id': boot, 'device': device,
              'nonce': secrets.token_hex(32), 'targets_deg': dict.fromkeys(TARGET_LIMITS),
              'observed_deg': dict.fromkeys(LIMITS_DEG), 'gripper_reference': None}
    path = os.path.abspath(path)
    directory = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if not _trusted(os.fstat(directory)):
            raise ValueError('Unsafe arm commissioning directory.')
        fd = os.open(os.path.basename(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(record, stream, indent=2, allow_nan=False)
            stream.write('\n')
    finally:
        os.close(directory)
    return {'template': path, 'device': metadata,
            'next_step': 'Fill independently confirmed same-session targets, observations and gripper reference.'}


def check(path, device):
    machine, boot = _identity()
    profile = _Profile(path, validate_device(device), lambda: (machine, boot))
    try:
        if profile.used:
            raise ValueError('Arm commissioning record already used; fresh commissioning required.')
        if not _access('.', os.W_OK | os.X_OK, dir_fd=profile.directory):
            raise ValueError('Service user cannot create a nonce marker in the commissioning directory.')
        metadata = _device_metadata(device)
        return {'commissioning': {'state': 'pending_preflight', 'profile_loaded': True, 'nonce_consumed': False},
                'machine_id': machine, 'boot_id': boot, 'device': metadata}
    finally:
        profile.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command')
    for command, description in (
            ('template', 'Create a new private template with empty operator reference fields.'),
            ('check', 'Validate a completed profile without opening serial or claiming its nonce.')):
        subparser = commands.add_parser(command, help=description)
        subparser.add_argument('path', help='profile path in an existing private directory')
        subparser.add_argument('--device', required=True, help='exact device path used by the service')
    args = parser.parse_args(argv)
    if args.command is None:
        parser.error('Choose template or check')
    if os.name != 'posix':
        print('R5 profile tooling requires the local Linux arm host.', file=sys.stderr)
        return 1
    try:
        result = template(args.path, args.device) if args.command == 'template' else check(args.path, args.device)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
