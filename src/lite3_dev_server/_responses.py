"""Validate known fields on v1 replies and preserve additive fields for callers."""
import math
from typing import Callable, List, Mapping, Optional, cast, get_args

from .api import (
    AcquireResponse, ArmFeedbackJoint, ArmJoint, ArmOperation, ArmReconnectResponse,
    ArmState, ControlMode, ControlStatus, DogActionName, DogActionStatus, DogPhase, Environment, ErrorResponse,
    VelocityAxis, VelocityLimit,
)


_Check = Callable[[object, str], None]
_Fields = Mapping[str, _Check]


def _object(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError('{} must be a JSON object'.format(path))
    return cast(Mapping[str, object], value)


def _shape(required: _Fields, optional: Optional[_Fields] = None) -> _Check:
    def check(value: object, path: str) -> None:
        fields = _object(value, path)
        for key, check_field in required.items():
            field_path = path + '.' + key
            if key not in fields:
                raise ValueError('{} is required'.format(field_path))
            check_field(fields[key], field_path)
        if optional is not None:
            for key, check_field in optional.items():
                if key in fields:
                    check_field(fields[key], path + '.' + key)
    return check


def _list(check_item: _Check) -> _Check:
    def check(value: object, path: str) -> None:
        if not isinstance(value, list):
            raise ValueError('{} must be a JSON array'.format(path))
        for index, item in enumerate(cast(List[object], value)):
            check_item(item, '{}[{}]'.format(path, index))
    return check


def _nullable(check_value: _Check) -> _Check:
    def check(value: object, path: str) -> None:
        if value is not None:
            check_value(value, path)
    return check


def _choice(*choices: str) -> _Check:
    def check(value: object, path: str) -> None:
        if not isinstance(value, str) or value not in choices:
            raise ValueError('{} has an unsupported value'.format(path))
    return check


def _literal(expected: Optional[bool]) -> _Check:
    def check(value: object, path: str) -> None:
        if value is not expected:
            raise ValueError('{} must be {}'.format(path, expected))
    return check


def _boolean(value: object, path: str) -> None:
    if not isinstance(value, bool):
        raise ValueError('{} must be a boolean'.format(path))


def _string(value: object, path: str) -> None:
    if not isinstance(value, str):
        raise ValueError('{} must be a string'.format(path))


def _lease(value: object, path: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError('{} must be a nonempty string'.format(path))


def _number(value: object, path: str) -> None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if math.isfinite(value):
                return
        except OverflowError:
            pass
    raise ValueError('{} must be a finite number'.format(path))


def _integer(value: object, path: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError('{} must be an integer'.format(path))


_ARM_DIAGNOSTIC_AXES = ('b', 'e', 'h')
_ARM_POSITION = _shape(dict.fromkeys(get_args(ArmJoint), _number))
_ARM_FEEDBACK = _shape(dict.fromkeys(get_args(ArmFeedbackJoint), _number))
_ARM_SAMPLE = _shape({'position_deg': _ARM_FEEDBACK, 'sample_monotonic': _number})
_ARM_OPERATION = _shape({
    'operation_id': _string,
    'phase': _choice('preflight', 'moving', 'stopping', 'completed', 'cancelled',
                     'fault', 'commanded_unverified'),
    'reason': _nullable(_string),
    'joint': _choice(*_ARM_DIAGNOSTIC_AXES),
    'delta_deg': _number,
    'speed_deg_s': _number,
    'observed_start': _nullable(_ARM_SAMPLE),
    'observed_end': _nullable(_ARM_SAMPLE),
    'targets_written': _list(_ARM_POSITION),
    'hold_status': _choice('not_needed', 'requested', 'written', 'unavailable'),
    'observed_stationary': _nullable(_boolean),
    'stop_verified': _literal(False),
    'gripper_position_deg': _literal(None),
})
_ARM_CAPABILITIES = _shape({
    'joints': _list(_choice(*_ARM_DIAGNOSTIC_AXES)),
    'max_delta_deg': _shape(dict.fromkeys(_ARM_DIAGNOSTIC_AXES, _number)),
    'max_speed_deg_s': _number,
    'max_travel_deg': _number,
    'asynchronous': _literal(True),
    'stop_verified': _literal(False),
    'gripper_feedback': _literal(False),
})
_ARM_FIELDS: _Fields = {
    'motion_enabled': _boolean,
    'reason': _nullable(_string),
    'position_deg': _nullable(_ARM_POSITION),
    'simulated': _boolean,
}
_ARM_OPTIONAL: _Fields = {
    'operation': _nullable(_ARM_OPERATION),
    'capabilities': _ARM_CAPABILITIES,
}
_DOG_FEEDBACK = _shape({
    'observed': _boolean,
    'simulated': _boolean,
    'fresh': _boolean,
    'age_s': _nullable(_number),
    'basic_state': _nullable(_integer),
    'gait_state': _nullable(_integer),
    'motion_state': _nullable(_integer),
    'standing': _boolean,
    'motion_ready': _boolean,
    'reason': _nullable(_string),
})
_DOG_ACTION = _shape({
    'id': _string,
    'name': _choice(*get_args(DogActionName)),
    'speed': _number,
    'duration': _number,
    'phase': _choice('running', 'completed', 'cancelled', 'fault'),
    'reason': _nullable(_string),
    'remaining_s': _number,
})
_DOG_STATUS = _shape({
    'phase': _choice(*get_args(DogPhase)),
    'velocity': _shape(dict.fromkeys(get_args(VelocityAxis), _number)),
    'limits': _shape(dict.fromkeys(get_args(VelocityLimit), _number)),
    'error': _nullable(_string),
    'stop_verified': _literal(False),
    'simulated': _boolean,
    'motion_enabled': _boolean,
    'feedback': _DOG_FEEDBACK,
    'action': _nullable(_DOG_ACTION),
    'actions_enabled': _boolean,
})
_CONTROL_FIELDS: _Fields = {
    'environment': _choice(*get_args(Environment)),
    'estop': _boolean,
    'safety_revision': _string,
    'lease_active': _boolean,
    'dog': _DOG_STATUS,
    'arm': _shape(_ARM_FIELDS, _ARM_OPTIONAL),
}
_CONTROL_STATUS = _shape(dict(_CONTROL_FIELDS, mode=_choice(*get_args(ControlMode))))
_ACQUIRE = _shape({'lease': _lease, 'status': _CONTROL_STATUS})
_OBSERVED_ARM = _shape({
    'observed': _literal(True),
    'position_deg': _ARM_FEEDBACK,
    'gripper_position_deg': _literal(None),
    'motion_enabled': _boolean,
    'reason': _nullable(_string),
}, {'sample_monotonic': _number})
_UNOBSERVED_ARM = _shape(dict(_ARM_FIELDS, observed=_literal(False)), _ARM_OPTIONAL)
_GATEWAY_RECONNECT = _shape({
    'connected': _literal(False),
    'reconnect_required': _literal(False),
    'motion_enabled': _literal(False),
    'reason': _string,
})
_ERROR = _shape({'error': _string}, {'code': _string})


def control_status(value: object) -> ControlStatus:
    _CONTROL_STATUS(value, 'response')
    return cast(ControlStatus, value)


def acquire(value: object) -> AcquireResponse:
    _ACQUIRE(value, 'response')
    return cast(AcquireResponse, value)


def arm_state(value: object) -> ArmState:
    fields = _object(value, 'response')
    check = _OBSERVED_ARM if fields.get('observed') is True else _UNOBSERVED_ARM
    check(value, 'response')
    return cast(ArmState, value)


def arm_reconnect(value: object) -> ArmReconnectResponse:
    fields = _object(value, 'response')
    check = _UNOBSERVED_ARM if 'observed' in fields else _GATEWAY_RECONNECT
    check(value, 'response')
    return cast(ArmReconnectResponse, value)


def arm_operation(value: object) -> ArmOperation:
    _ARM_OPERATION(value, 'response')
    return cast(ArmOperation, value)


def dog_action_status(value: object) -> DogActionStatus:
    _DOG_ACTION(value, 'response')
    return cast(DogActionStatus, value)


def error_response(value: object) -> ErrorResponse:
    _ERROR(value, 'response')
    return cast(ErrorResponse, value)
