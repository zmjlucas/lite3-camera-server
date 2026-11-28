"""Content identity for deployed files and the process that loaded them."""
import os
from pathlib import Path
import sys

if __package__:
    from .build_info import build_id, read_bundle
else:
    from build_info import build_id, read_bundle


def bundle_version(directory):
    """Hash the allowlisted bundle. Partial installations have no complete hash."""
    files, unavailable = read_bundle(Path(directory))
    return {'sha256': None if unavailable else build_id(files).split(':', 1)[1],
            'unavailable': unavailable}


def runtime_identity(options, directory=None):
    """Capture once at startup, before a later deployment can replace the files."""
    directory = Path(directory) if directory is not None else Path(__file__).resolve().parent
    config = {name: getattr(options, name, None) for name in (
        'source', 'device', 'control_mode', 'env', 'simulate', 'dog_host',
        'dog_state_port', 'arm_device', 'arm_host',
    )}
    config['arm_diagnostics'] = bool(getattr(options, 'arm_motion_profile', None))
    identity = None
    if hasattr(os, 'geteuid'):
        identity = {'uid': os.geteuid(), 'gid': os.getegid(), 'groups': os.getgroups()}
    return {'version': bundle_version(directory), 'directory': str(directory),
            'pid': os.getpid(), 'python': sys.executable, 'python_version': sys.version.split()[0],
            'identity': identity, 'config': config}
