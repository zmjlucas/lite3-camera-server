"""Read-only action decisions from a validated snapshot and explicit intent."""
from typing import List, Mapping, Optional, get_args

from . import _responses
from .api import (
    ControlStatus, DogActionName, MotionOperation, PreflightReason,
    PreflightResult, TargetTransport,
)
from .validation import _validate_parameters


def check_preflight(status: ControlStatus, operation: MotionOperation, target: TargetTransport, *,
                    owns_lease: bool = False,
                    parameters: Optional[Mapping[str, object]] = None) -> PreflightResult:
    """Return blockers without I/O or mutation; a snapshot grants no ownership.

    ``owns_lease`` means the caller holds the private lease from acquisition.
    Check acquisition separately from preparation and action readiness. An
    acquisition response is checked with ``owns_lease=True``; a prospective
    acquisition uses False. DevClient.preflight always checks prospective work.
    The server validates the current state again when a command arrives.
    ``parameters=None`` checks readiness; a mapping also checks request values.
    """
    if target not in get_args(TargetTransport):
        raise ValueError('target must be simulation or hardware')
    if operation not in get_args(MotionOperation):
        raise ValueError('Unsupported preflight operation')
    if not isinstance(owns_lease, bool):
        raise ValueError('owns_lease must be boolean')
    status = _responses.control_status(status)
    reasons: List[PreflightReason] = []

    def block(code: str, message: str) -> None:
        reasons.append({'code': code, 'message': message})

    if status['mode'] != 'hardware':
        block('control_' + status['mode'], 'Control mode is ' + status['mode'])
    if target == 'simulation' and status['environment'] != 'dev':
        block('environment_mismatch', 'Simulation requires the dev environment')
    if status['estop']:
        block('estop_latched', 'Software emergency stop is latched; inspect status and reset explicitly')
    dog, arm = status['dog'], status['arm']
    if dog['error'] is not None:
        block('control_error', dog['error'])
    devices = ('dog', 'arm') if operation == 'acquire' else (operation.split('/')[0],)
    for device in devices:
        simulated = dog['simulated'] if device == 'dog' else arm['simulated']
        if simulated != (target == 'simulation'):
            block('transport_mismatch', '{} transport does not match {}'.format(device, target))

    if operation == 'acquire' and not owns_lease:
        if status['lease_active']:
            block('lease_busy', 'Control already has an active lease')
    elif not owns_lease or not status['lease_active']:
        block('lease_required', 'An explicitly acquired, active lease is required')

    active_arm = arm.get('operation')
    arm_busy = active_arm is not None and active_arm['phase'] in ('preflight', 'moving', 'stopping')
    if operation == 'acquire':
        if dog['phase'] == 'stopping':
            block('stop_in_progress', 'Wait for the bounded dog stop attempt')
        if arm_busy:
            block('arm_busy', 'An arm operation is still active')
    elif operation.startswith('dog/'):
        if not dog['motion_enabled']:
            block('capability_unavailable', 'Dog output is unavailable')
        action = dog['action']
        if action is not None and action['phase'] == 'running':
            block('action_running', 'Stop the current timed dog action before further motion')
        if operation == 'dog/stand':
            if dog['phase'] != 'disarmed':
                block('dog_not_disarmed', 'Stop and acquire again before preparing the dog')
        elif operation == 'dog/velocity':
            if dog['phase'] not in ('ready', 'moving'):
                block('dog_not_ready', 'Explicit dog preparation is required')
        else:
            if dog['phase'] != 'ready':
                block('dog_not_ready', 'Wait for idle readiness after explicit dog preparation')
            caps = dog.get('capabilities')
            if (not dog['actions_enabled'] or caps is None
                    or not any(name in caps['actions'] for name in get_args(DogActionName))):
                block('capability_unavailable', 'Timed dog actions or their limits are unavailable')
            feedback = dog['feedback']
            if feedback['simulated'] != (target == 'simulation'):
                block('transport_mismatch', 'Dog feedback transport does not match ' + target)
            if (not feedback['fresh'] or not feedback['standing'] or not feedback['motion_ready']
                    or feedback['observed'] != (target == 'hardware')):
                block('feedback_unavailable', feedback['reason'] or 'Fresh stable standing feedback is required')
    else:
        if status['environment'] != 'dev':
            block('environment_mismatch', 'Arm motion requires the dev environment')
        if operation == 'arm/action':
            if target != 'simulation' or not arm['motion_enabled']:
                block('capability_unavailable', arm['reason'] or 'Arm target actions require simulation')
        else:
            arm_caps = arm.get('capabilities')
            if (not arm['motion_enabled'] or (target == 'hardware' and arm_caps is None)
                    or (arm_caps is not None and not arm_caps['joints'])):
                block('capability_unavailable', arm['reason'] or 'Arm jog capability is unavailable')
        if arm_busy:
            block('arm_busy', 'An arm operation is still active')

    if parameters is not None:
        try:
            _validate_parameters(operation, parameters, status)
        except ValueError as error:
            block('invalid_parameters', str(error))
    return {'ok': not reasons, 'operation': operation, 'target': target,
            'reasons': reasons, 'status': status}
