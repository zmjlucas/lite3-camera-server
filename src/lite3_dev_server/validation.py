"""Pure parameter rules for SDK and CLI callers; the server remains authoritative."""
import math
from typing import Dict, Literal, Mapping, Optional, Tuple, cast

from . import _responses
from .api import (
    ArmCapabilities, ArmJoint, ControlStatus, DogActionName, DogCapabilities,
    MotionOperation, VelocityAxis, VelocityLimit,
)


_VELOCITY_LIMITS: Dict[VelocityLimit, float] = {'vx': .2, 'vy': .1, 'yaw': .3, 'ttl': .5}
_ACTION_AXES: Dict[DogActionName, Tuple[VelocityAxis, int]] = {
    'forward': ('vx', 1), 'backward': ('vx', -1),
    'turn_left': ('yaw', -1), 'turn_right': ('yaw', 1),
}
_JOINT_LIMITS: Dict[ArmJoint, float] = {'b': 180., 's': 90., 'e': 90., 'w': 90., 'h': 45.}


def _finite(value: object, field: str) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if math.isfinite(value):
                return float(value)
        except OverflowError:
            pass
    raise ValueError('{} must be a finite number'.format(field))


def _maximum(value: object, field: str) -> float:
    maximum = _finite(value, field)
    if maximum <= 0:
        raise ValueError('{} must be positive'.format(field))
    return maximum


def _range(value: object, field: str, low: float, high: float, positive: bool = False) -> float:
    number = _finite(value, field)
    if not low <= number <= high or (positive and number <= 0):
        raise ValueError('{} is outside the permitted range ({}{} to {})'.format(
            field, 'greater than ' if positive else '', low, high))
    return number


def _limits(actual: Optional[Mapping[VelocityLimit, float]]) -> Dict[VelocityLimit, float]:
    limits = dict(_VELOCITY_LIMITS)
    if actual is not None:
        for axis, maximum in limits.items():
            if axis not in actual:
                raise ValueError('Missing {} limit'.format(axis))
            limits[axis] = min(maximum, _maximum(actual[axis], axis + ' limit'))
    return limits


def validate_dog_action(name: DogActionName, speed: float, duration: float, *,
                        capabilities: Optional[DogCapabilities] = None,
                        limits: Optional[Mapping[VelocityLimit, float]] = None) -> Dict[VelocityAxis, float]:
    """Validate positive speed/duration and return the native velocity mapping.

    Defaults match the shipped offline contract: translation <= .2 m/s, turns
    <= .3 rad/s, duration <= 10 s. Advertised limits can tighten these bounds.
    Invalid parameters or capability metadata raise ValueError without I/O.
    """
    if not isinstance(name, str) or name not in _ACTION_AXES:
        raise ValueError('Unsupported dog action')
    axis, sign = _ACTION_AXES[name]
    max_speed = _limits(limits)[axis]
    max_duration = 10.
    if capabilities is not None:
        caps = _responses.dog_capabilities(capabilities)
        if name not in caps['actions']:
            raise ValueError('Dog action is unavailable: ' + name)
        max_speed = min(max_speed, caps['actions'][name]['max_speed'])
        max_duration = min(max_duration, caps['max_duration'])
    speed = _range(speed, 'speed', 0., max_speed, positive=True)
    _range(duration, 'duration', 0., max_duration, positive=True)
    velocity: Dict[VelocityAxis, float] = {'vx': 0., 'vy': 0., 'yaw': 0.}
    velocity[axis] = sign * speed
    return velocity


def validate_velocity(vx: float = 0., vy: float = 0., yaw: float = 0., ttl: float = .3, *,
                      limits: Optional[Mapping[VelocityLimit, float]] = None) -> None:
    """Validate velocity/TTL offline or against tighter service limits."""
    maxima = _limits(limits)
    values: Dict[VelocityAxis, float] = {'vx': vx, 'vy': vy, 'yaw': yaw}
    for axis, value in values.items():
        _range(value, axis, -maxima[axis], maxima[axis])
    _range(ttl, 'ttl', 0., maxima['ttl'], positive=True)


def validate_arm_jog(joint: ArmJoint, delta: float, speed: float = 2., *,
                     capabilities: Optional[ArmCapabilities] = None,
                     position_deg: Optional[Mapping[ArmJoint, float]] = None) -> None:
    """Validate a bounded jog, optionally using diagnostic limits/current position."""
    if not isinstance(joint, str) or joint not in _JOINT_LIMITS:
        raise ValueError('Unsupported arm joint')
    max_delta, max_speed = 2., 5.
    if capabilities is not None:
        _responses._ARM_CAPABILITIES(capabilities, 'capabilities')
        if joint not in capabilities['joints']:
            raise ValueError('Arm joint is unavailable: ' + joint)
        diagnostic_joint = cast(Literal['b', 'e', 'h'], joint)
        max_delta = min(max_delta, _maximum(capabilities['max_delta_deg'][diagnostic_joint], 'delta limit'))
        max_speed = min(max_speed, _maximum(capabilities['max_speed_deg_s'], 'speed limit'))
    delta = _range(delta, 'delta', -max_delta, max_delta)
    speed = _range(speed, 'speed', 0., max_speed, positive=True)
    if capabilities is not None and (abs(delta) < .1 or abs(delta) > 5. * speed):
        raise ValueError('Diagnostic jog requires at least 0.1 degrees and at most 5 seconds of travel')
    if position_deg is not None:
        if joint not in position_deg:
            raise ValueError('Missing current joint position')
        target = _finite(position_deg[joint], 'joint position') + delta
        _range(target, 'joint target', -_JOINT_LIMITS[joint], _JOINT_LIMITS[joint])


def _validate_parameters(operation: MotionOperation, parameters: Mapping[str, object],
                         status: Optional[ControlStatus] = None) -> None:
    fields = {
        'acquire': (), 'dog/stand': (), 'dog/velocity': ('vx', 'vy', 'yaw', 'ttl'),
        'dog/action': ('name', 'speed', 'duration'), 'arm/jog': ('joint', 'delta', 'speed'),
        'arm/action': ('targets', 'duration'),
    }
    if set(parameters) != set(fields[operation]):
        raise ValueError('Unexpected or missing motion parameters')
    if operation == 'dog/action':
        validate_dog_action(cast(DogActionName, parameters['name']),
                            cast(float, parameters['speed']), cast(float, parameters['duration']),
                            capabilities=status['dog'].get('capabilities') if status else None,
                            limits=status['dog']['limits'] if status else None)
    elif operation == 'dog/velocity':
        validate_velocity(cast(float, parameters['vx']), cast(float, parameters['vy']),
                          cast(float, parameters['yaw']), cast(float, parameters['ttl']),
                          limits=status['dog']['limits'] if status else None)
    elif operation == 'arm/jog':
        validate_arm_jog(cast(ArmJoint, parameters['joint']), cast(float, parameters['delta']),
                         cast(float, parameters['speed']),
                         capabilities=status['arm'].get('capabilities') if status else None,
                         position_deg=status['arm']['position_deg'] if status else None)
    elif operation == 'arm/action':
        targets = parameters['targets']
        if not isinstance(targets, dict) or not targets or set(targets) - set(_JOINT_LIMITS):
            raise ValueError('targets must contain supported arm joints')
        _range(parameters['duration'], 'duration', 0., 10., positive=True)
        for joint, value in cast(Dict[ArmJoint, object], targets).items():
            _range(value, joint, -_JOINT_LIMITS[joint], _JOINT_LIMITS[joint])
