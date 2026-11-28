"""Opt-in, bounded action execution over the v1 HTTP transport.

Each step has a private lease and a renewal worker. Motion is submitted once;
uncertain results end execution. Software completion retains server evidence
about simulation, observations and unverified physical stopping.
"""
from dataclasses import dataclass, replace
import math
import threading
import time
from types import TracebackType
from typing import Callable, List, Literal, Mapping, Optional, Sequence, Tuple, Type, TypeVar, Union, cast

from . import _responses
from .api import ArmDiagnosticOperation, ArmJoint, ArmOperation, ControlStatus, DogActionName
from .client import DevClient, DevClientError
from .validation import validate_arm_jog, validate_dog_action


ManagedPhase = Literal['completed', 'cancelled', 'fault', 'timed_out', 'unknown', 'commanded_unverified']
LeasePolicy = Literal['fail', 'wait']
SequencePolicy = Literal['completed_only']
_Response = TypeVar('_Response')
_TERMINAL = ('completed', 'cancelled', 'fault', 'commanded_unverified')
_JOINT_LIMITS = {'b': 180.0, 's': 90.0, 'e': 90.0, 'w': 90.0, 'h': 45.0}


@dataclass(frozen=True)
class DogAction:
    """Timed translation (m/s) or turn (rad/s), with a positive speed."""

    name: DogActionName
    speed: float = 0.1
    duration: float = 1.0


@dataclass(frozen=True)
class ArmJog:
    """Relative joint degrees and degrees/second; hardware requires commissioning."""

    joint: ArmJoint
    delta: float
    speed: float = 1.0


ManagedAction = Union[DogAction, ArmJog]


@dataclass(frozen=True)
class ActionResult:
    """Outcome with unchanged server evidence; physical stop stays unverified."""

    phase: ManagedPhase
    action_id: Optional[str] = None
    reason: Optional[str] = None
    status: Optional[ControlStatus] = None
    operation: Optional[ArmOperation] = None
    cleanup_error: Optional[str] = None


@dataclass(frozen=True)
class SequenceResult:
    """Attempted steps in order; capability preflight failures have no steps."""

    phase: ManagedPhase
    results: Tuple[ActionResult, ...] = ()
    reason: Optional[str] = None


class _Abort(Exception):
    def __init__(self, phase: ManagedPhase, reason: str) -> None:
        self.phase = phase
        self.reason = reason


class _HeartbeatFailure(Exception):
    def __init__(self, error: DevClientError) -> None:
        self.error = error


def _number(value: float, name: str, maximum: Optional[float] = None) -> None:
    valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    try:
        valid = valid and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid or value <= 0 or (maximum is not None and value > maximum):
        raise ValueError('{} is outside the permitted finite range'.format(name))


def _validate(action: ManagedAction) -> None:
    if isinstance(action, DogAction):
        validate_dog_action(action.name, action.speed, action.duration)
    elif isinstance(action, ArmJog):
        validate_arm_jog(action.joint, action.delta, action.speed)
    else:
        raise ValueError('Expected a DogAction or ArmJog')


def _available(state: ControlStatus) -> None:
    if state['mode'] != 'hardware':
        raise _Abort('fault', 'Target control mode does not permit actions')
    if state['estop'] or state['dog']['error']:
        raise _Abort('fault', 'Control is latched; explicit caller reset is required')


def _capabilities(actions: Sequence[ManagedAction], state: ControlStatus) -> None:
    _available(state)
    positions = dict(state['arm']['position_deg'] or {})
    for action in actions:
        if isinstance(action, DogAction):
            if not state['dog']['motion_enabled']:
                raise _Abort('fault', 'Dog output is disabled on this target')
            axis: Literal['vx', 'yaw'] = 'vx' if action.name in ('forward', 'backward') else 'yaw'
            if action.speed > state['dog']['limits'][axis]:
                raise _Abort('fault', 'Dog speed exceeds the target capability')
        else:
            arm = state['arm']
            if state['environment'] != 'dev' or not arm['motion_enabled']:
                raise _Abort('fault', arm['reason'] or 'Arm motion is disabled on this target')
            if arm['simulated']:
                if action.joint not in positions:
                    raise _Abort('fault', 'Simulated arm target is unavailable')
                positions[action.joint] += action.delta
                if abs(positions[action.joint]) > _JOINT_LIMITS[action.joint]:
                    raise _Abort('fault', 'Simulated arm sequence exceeds joint limits')
            else:
                caps = arm.get('capabilities')
                if caps is None or state['dog']['motion_enabled']:
                    raise _Abort('fault', 'Arm diagnostics require capabilities and isolated dog output')
                if action.joint not in caps['joints']:
                    raise _Abort('fault', 'Arm joint is unavailable on this target')
                joint = cast(Literal['b', 'e', 'h'], action.joint)
                if (abs(action.delta) < 0.1 or
                        abs(action.delta) > caps['max_delta_deg'][joint] or
                        action.speed > caps['max_speed_deg_s'] or
                        abs(action.delta) > 5.0 * action.speed):
                    raise _Abort('fault', 'Arm jog exceeds the target diagnostic capability')


class _Execution:
    def __init__(self, timeout: float) -> None:
        self.deadline = time.monotonic() + timeout
        self.cancelled = threading.Event()

    def check(self) -> None:
        if self.cancelled.is_set():
            raise _Abort('cancelled', 'Managed execution was cancelled')
        if time.monotonic() >= self.deadline:
            raise _Abort('timed_out', 'Managed execution deadline elapsed')

    def pause(self, interval: float) -> None:
        self.check()
        self.cancelled.wait(min(interval, max(0.0, self.deadline - time.monotonic())))
        self.check()


class _Lease:
    """One immutable capability; worker lifetime ends before cleanup or reacquisition."""

    def __init__(self, client: DevClient, token: str, execution: _Execution,
                 interval: float, request_timeout: float) -> None:
        self.token = token
        self.sequence = 0
        self._client = client
        self._execution = execution
        self._interval = interval
        self._request_timeout = request_timeout
        self._stop = threading.Event()
        self._error_lock = threading.Lock()
        self._error: Optional[DevClientError] = None
        self._thread = threading.Thread(target=self._renew, name='lite3-managed-renewal', daemon=True)
        self._thread.start()

    @property
    def error(self) -> Optional[DevClientError]:
        with self._error_lock:
            return self._error

    def _renew(self) -> None:
        while not self._stop.wait(self._interval):
            remaining = self._execution.deadline - time.monotonic()
            if remaining <= 0 or self._execution.cancelled.is_set():
                return
            try:
                self._client._request('control/heartbeat', _responses.control_status,
                                      {'lease': self.token}, timeout=min(self._request_timeout, remaining))
            except DevClientError as error:
                with self._error_lock:
                    self._error = error
                return

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()


class _Step:
    def __init__(self, action: ManagedAction) -> None:
        self.action = action
        self.token: Optional[str] = None
        self.lease: Optional[_Lease] = None
        self.action_id: Optional[str] = None
        self.status: Optional[ControlStatus] = None
        self.operation: Optional[ArmOperation] = None

    def check(self, execution: _Execution) -> None:
        execution.check()
        error = self.lease.error if self.lease else None
        if error is not None:
            raise _HeartbeatFailure(error)

    def result(self, phase: ManagedPhase, reason: Optional[str] = None) -> ActionResult:
        return ActionResult(phase, self.action_id, reason, self.status, self.operation)

    def dog_terminal(self) -> bool:
        if not isinstance(self.action, DogAction) or self.status is None or self.action_id is None:
            return False
        action = self.status['dog']['action']
        return action is not None and action['id'] == self.action_id and action['phase'] in _TERMINAL


class ManagedClient:
    """Run explicitly requested actions with bounded preparation, renewal and cleanup.

    `run_sequence` requires policy='completed_only'. Each step gets a fresh lease.
    lease_policy='fail' rejects another owner; 'wait' waits within the call deadline.
    cancel() signals the current call, and close() waits for its scoped cleanup.
    Use one active call per client. A new explicit call can start a new execution;
    an interrupted call never resumes itself.
    """

    def __init__(self, url: str, *, owner: str = 'python-managed-sdk',
                 request_timeout: float = 0.5, poll_interval: float = 0.05,
                 heartbeat_interval: float = 0.4, lease_policy: LeasePolicy = 'fail') -> None:
        if (not isinstance(owner, str) or not owner.strip() or len(owner) > 80 or
                any(ord(char) < 32 for char in owner)):
            raise ValueError('owner must be a short nonempty label')
        _number(request_timeout, 'request_timeout', 1.0)
        _number(poll_interval, 'poll_interval', 0.5)
        _number(heartbeat_interval, 'heartbeat_interval', 0.5)
        if lease_policy not in ('fail', 'wait'):
            raise ValueError('lease_policy must be fail or wait')
        self._client = DevClient(url, timeout=request_timeout)
        self._owner = owner
        self._request_timeout = request_timeout
        self._poll_interval = poll_interval
        self._heartbeat_interval = heartbeat_interval
        self._lease_policy = lease_policy
        self._condition = threading.Condition()
        self._active: Optional[_Execution] = None
        self._closed = False

    def _request(self, execution: _Execution, path: str,
                 validate: Callable[[object], _Response],
                 body: Optional[Mapping[str, object]] = None) -> _Response:
        execution.check()
        remaining = execution.deadline - time.monotonic()
        return self._client._request(path, validate, body, timeout=min(self._request_timeout, max(0.001, remaining)))

    def run(self, action: ManagedAction, *, timeout: float = 30.0) -> ActionResult:
        """Prepare, submit once and await one action; timeout includes preparation."""
        result = self.run_sequence((action,), policy='completed_only', timeout=timeout)
        if result.results:
            return result.results[-1]
        return ActionResult(result.phase, reason=result.reason)

    def run_sequence(self, actions: Sequence[ManagedAction], *, policy: SequencePolicy,
                     timeout: float = 60.0) -> SequenceResult:
        """Validate every step first; advance only after confirmed completed results."""
        if policy != 'completed_only':
            raise ValueError('policy must be completed_only')
        _number(timeout, 'timeout')
        steps = tuple(actions)
        if not steps:
            raise ValueError('actions must contain at least one step')
        for action in steps:
            _validate(action)
        with self._condition:
            if self._closed or self._active is not None:
                raise RuntimeError('Managed client is closed or already executing')
            execution = _Execution(timeout)
            self._active = execution
        results: List[ActionResult] = []
        try:
            state = self._request(execution, 'status', _responses.control_status)
            execution.check()
            _capabilities(steps, state)
            for action in steps:
                execution.check()
                result = self._run_step(action, execution)
                results.append(result)
                if result.phase != 'completed':
                    return SequenceResult(result.phase, tuple(results), result.reason)
            return SequenceResult('completed', tuple(results))
        except _Abort as error:
            return SequenceResult(error.phase, tuple(results), error.reason)
        except DevClientError as error:
            return SequenceResult('unknown', tuple(results), str(error))
        finally:
            with self._condition:
                self._active = None
                self._condition.notify_all()

    def _acquire(self, step: _Step, execution: _Execution) -> None:
        while True:
            step.status = self._request(execution, 'status', _responses.control_status)
            execution.check()
            _capabilities((step.action,), step.status)
            operation = step.status['arm'].get('operation')
            stopping = (step.status['dog']['phase'] == 'stopping' or
                        (operation is not None and operation['phase'] in ('preflight', 'moving', 'stopping')))
            if step.status['lease_active']:
                if self._lease_policy == 'fail':
                    raise _Abort('fault', 'Another controller holds the lease')
            elif not stopping:
                try:
                    reply = self._request(execution, 'control/acquire', _responses.acquire, {'owner': self._owner})
                except DevClientError as error:
                    if error.status != 409:
                        raise
                    # A rejected acquisition created no capability. Re-read occupancy
                    # to distinguish a competing owner from stop recovery.
                else:
                    step.token = reply['lease']
                    step.status = reply['status']
                    execution.check()
                    _capabilities((step.action,), step.status)
                    step.lease = _Lease(self._client, step.token, execution,
                                        self._heartbeat_interval, self._request_timeout)
                    return
            execution.pause(self._poll_interval)

    def _motion(self, step: _Step, execution: _Execution, path: str,
                values: Optional[Mapping[str, object]] = None) -> ControlStatus:
        step.check(execution)
        assert step.lease is not None
        step.lease.sequence += 1
        body = dict(values or {})
        body.update(lease=step.lease.token, seq=step.lease.sequence)
        return self._request(execution, path, _responses.control_status, body)

    def _prepare_dog(self, step: _Step, execution: _Execution) -> None:
        step.status = self._motion(step, execution, 'dog/stand')
        while True:
            step.check(execution)
            assert step.status is not None
            _capabilities((step.action,), step.status)
            dog = step.status['dog']
            if (dog['phase'] == 'ready' and dog['actions_enabled'] and
                    dog['feedback']['fresh'] and dog['feedback']['standing']):
                return
            if dog['phase'] not in ('preparing', 'ready') or not step.status['lease_active']:
                raise _Abort('fault', 'Dog preparation lost readiness or ownership')
            execution.pause(self._poll_interval)
            step.check(execution)
            step.status = self._request(execution, 'status', _responses.control_status)

    def _submit(self, step: _Step, execution: _Execution) -> Optional[ActionResult]:
        action = step.action
        if isinstance(action, DogAction):
            self._prepare_dog(step, execution)
            assert step.status is not None
            previous = step.status['dog']['action']
            step.status = self._motion(step, execution, 'dog/action',
                                       {'name': action.name, 'speed': action.speed, 'duration': action.duration})
            accepted = step.status['dog']['action']
            if (accepted is None or not accepted['id'] or
                    (previous is not None and accepted['id'] == previous['id']) or
                    (accepted['name'], accepted['speed'], accepted['duration']) !=
                    (action.name, action.speed, action.duration)):
                raise _Abort('unknown', 'Dog submission did not acknowledge a new matching action')
            step.action_id = accepted['id']
        else:
            step.check(execution)
            step.status = self._request(execution, 'status', _responses.control_status)
            _capabilities((action,), step.status)
            previous_arm = step.status['arm'].get('operation')
            simulated = step.status['arm']['simulated']
            positions = step.status['arm']['position_deg']
            target = positions[action.joint] + action.delta if simulated and positions is not None else None
            step.status = self._motion(step, execution, 'arm/jog',
                                       {'joint': action.joint, 'delta': action.delta, 'speed': action.speed})
            if simulated:
                returned = step.status['arm']
                if (not returned['simulated'] or returned['position_deg'] is None or
                        returned['position_deg'][action.joint] != target):
                    raise _Abort('unknown', 'Simulated arm submission has an unexpected target')
                step.check(execution)
                return step.result('completed')
            step.operation = step.status['arm'].get('operation')
            operation = step.operation
            if (operation is None or 'kind' in operation or not operation['operation_id'] or
                    (previous_arm is not None and operation['operation_id'] == previous_arm['operation_id'])):
                raise _Abort('unknown', 'Arm submission did not acknowledge a new matching operation')
            diagnostic = cast(ArmDiagnosticOperation, operation)
            if ((diagnostic['joint'], diagnostic['delta_deg'], diagnostic['speed_deg_s']) !=
                    (action.joint, action.delta, action.speed)):
                raise _Abort('unknown', 'Arm submission did not acknowledge a new matching operation')
            step.action_id = operation['operation_id']
        step.check(execution)
        return self._terminal(step)

    @staticmethod
    def _terminal(step: _Step) -> Optional[ActionResult]:
        phase: str
        if isinstance(step.action, DogAction):
            action = step.status['dog']['action'] if step.status is not None else None
            if action is None or action['id'] != step.action_id:
                raise _Abort('unknown', 'Acknowledged dog action is missing or superseded')
            phase, reason = action['phase'], action['reason']
        else:
            operation = step.operation
            if operation is None or operation['operation_id'] != step.action_id:
                raise _Abort('unknown', 'Acknowledged arm operation is missing or superseded')
            phase, reason = operation['phase'], operation['reason']
        if phase in _TERMINAL:
            return step.result(cast(ManagedPhase, phase), reason)
        return None

    def _read(self, step: _Step, execution: _Execution) -> Optional[ActionResult]:
        if isinstance(step.action, DogAction):
            step.status = self._request(execution, 'status', _responses.control_status)
        else:
            step.operation = self._request(execution, 'arm/operation', _responses.arm_operation,
                                           {'id': step.action_id})
        execution.check()
        return self._terminal(step)

    def _reconcile(self, step: _Step, execution: _Execution, error: DevClientError) -> ActionResult:
        if error.status == 409 and step.action_id is not None:
            result = self._read(step, execution)
            if result is not None:
                return result
        raise _Abort('unknown', 'Lease renewal failed: {}'.format(error))

    def _perform(self, step: _Step, execution: _Execution) -> ActionResult:
        self._acquire(step, execution)
        result = self._submit(step, execution)
        while result is None:
            execution.pause(self._poll_interval)
            step.check(execution)
            result = self._read(step, execution)
            step.check(execution)
        return result

    def _run_step(self, action: ManagedAction, execution: _Execution) -> ActionResult:
        step = _Step(action)
        result: Optional[ActionResult] = None
        try:
            try:
                result = self._perform(step, execution)
            except _HeartbeatFailure as failure:
                result = self._reconcile(step, execution, failure.error)
            # Join before accepting completion, so a late renewal failure cannot
            # leak into another step or silently permit continuation.
            if step.lease is not None:
                step.lease.stop()
                error = step.lease.error
                if error is not None and result.phase == 'completed':
                    result = self._reconcile(step, execution, error)
            execution.check()
        except _Abort as error:
            result = step.result(error.phase, error.reason)
        except DevClientError as error:
            phase: ManagedPhase = 'fault' if error.status in (400, 403, 409, 423) else 'unknown'
            result = step.result(phase, str(error))
        finally:
            if step.lease is not None:
                step.lease.stop()
            if step.token is not None and not step.dog_terminal():
                try:
                    released = self._client._request('control/release', _responses.control_status,
                                                     {'lease': step.token}, timeout=self._request_timeout)
                    if result is not None and result.phase in ('cancelled', 'timed_out', 'fault', 'unknown'):
                        operation = released['arm'].get('operation')
                        if operation is None or operation['operation_id'] != step.action_id:
                            operation = result.operation
                        result = replace(result, status=released, operation=operation)
                except DevClientError as error:
                    if result is not None and error.status != 409:
                        result = replace(result, cleanup_error=str(error),
                                         phase='unknown' if result.phase == 'completed' else result.phase)
        assert result is not None
        if result.phase == 'completed':
            try:
                execution.check()
            except _Abort as error:
                result = replace(result, phase=error.phase, reason=error.reason)
        return result

    def cancel(self) -> None:
        """Request cancellation of the current call; cleanup uses only its lease."""
        with self._condition:
            if self._active is not None:
                self._active.cancelled.set()

    def close(self) -> None:
        """Cancel an active call and wait for its renewal worker and cleanup."""
        with self._condition:
            self._closed = True
            if self._active is not None:
                self._active.cancelled.set()
            while self._active is not None:
                self._condition.wait()

    def __enter__(self) -> 'ManagedClient':
        with self._condition:
            if self._closed:
                raise RuntimeError('Managed client is closed')
        return self

    def __exit__(self, exception_type: Optional[Type[BaseException]],
                 exception: Optional[BaseException], traceback: Optional[TracebackType]) -> Literal[False]:
        self.close()
        return False
