"""Small synchronous Lite3 Dev Server SDK (Python 3.8+, standard library only).

No operation retries or acquires ownership automatically. Explicit operation waits
can renew an existing lease. Velocity TTL is independent of that lease heartbeat.
"""
from http.client import HTTPException, HTTPMessage
import json
import math
import threading
import time
from types import TracebackType
from typing import IO, Callable, Dict, List, Literal, Mapping, Optional, Sequence, Tuple, Type, TypeVar, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from . import _responses
from ._transport import DeadlineHTTPHandler, DeadlineHTTPSHandler
from .api import (
    AcquireRequest, AcquireResponse, ArmJoint, ArmReconnectResponse, ArmState,
    ArmOperation, ArmOperationRequest,
    ControlStatus, DogActionName, DogActionStatus, DogActionStatusRequest, LeaseRequest,
    MotionOperation, PreflightResult, PROTOCOL_HEADER, PROTOCOL_VERSION, ResetRequest,
    StopRequest, TargetTransport,
)
from .preflight import check_preflight
from .validation import _validate_parameters


_Response = TypeVar('_Response')


class _WaitDeadlineExpired(TimeoutError):
    """Internal transport deadline; the waiter attaches its trusted snapshot."""


class DevClientError(RuntimeError):
    """The server rejected a request, or its response could not be trusted."""

    status: Optional[int]
    code: Optional[str]

    def __init__(self, message: str, status: Optional[int] = None,
                 code: Optional[str] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class PreflightError(DevClientError):
    """Local blockers prevented a guarded request; inspect ``result['reasons']``."""

    def __init__(self, result: PreflightResult) -> None:
        super().__init__('Preflight blocked: ' + '; '.join(reason['message'] for reason in result['reasons']))
        self.result = result


class DevClientTimeout(DevClientError, TimeoutError):
    """An explicit wait expired; operation holds its last received snapshot."""

    def __init__(self, operation_id: str, operation: Optional[ArmOperation]) -> None:
        super().__init__('Timed out waiting for arm operation ' + operation_id)
        self.operation_id = operation_id
        self.operation = operation


def _positive_seconds(name: str, value: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if math.isfinite(value) and value > 0:
                return float(value)
        except OverflowError:
            pass
    raise ValueError(name + ' must be finite and greater than zero')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: IO[bytes], code: int, msg: str,
                         headers: HTTPMessage, newurl: str) -> None:
        # Send robot commands only to the configured service.
        return None


def _check_protocol(versions: Optional[Sequence[str]]) -> None:
    if versions is None or len(versions) != 1 or versions[0].strip(' \t') != PROTOCOL_VERSION:
        raise DevClientError('Missing, unsupported or ambiguous Lite3 Dev protocol version')


def _parse_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('Non-finite JSON number')
    return number


def _unique_json_object(pairs: List[Tuple[str, object]]) -> Dict[str, object]:
    fields: Dict[str, object] = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError('Duplicate JSON field')
        fields[key] = value
    return fields


class DevClient:
    """Explicit API calls; context exit releases an acquired lease.

    Stops, resets, observed terminal dog actions and failed control requests forget
    ownership for their own session. Action-ID lookups leave ownership unchanged.
    Reacquire and explicitly prepare the dog again before further motion. A
    successful response is software state, never proof of physical robot state.
    Motion calls are serialized; reads, heartbeats and stops can run concurrently.
    Replies from an earlier ownership session cannot change the current session.
    """

    MAX_RESPONSE = 65536

    def __init__(self, url: str, timeout: float = 2.0, *,
                 target: Optional[TargetTransport] = None) -> None:
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or
                parsed.query or parsed.fragment or parsed.path not in ('', '/')):
            raise ValueError('url must be an HTTP(S) server origin without credentials, query or path')
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
                not math.isfinite(timeout) or not 0 < timeout <= 10):
            raise ValueError('timeout must be finite, positive and at most 10 seconds')
        if target is not None and target not in ('simulation', 'hardware'):
            raise ValueError('target must be simulation or hardware')
        self.url = url.rstrip('/')
        self.timeout = float(timeout)
        self._target = target
        self._lease: Optional[str] = None
        self._dog_action_id: Optional[str] = None
        self._arm_operation_id: Optional[str] = None
        self._sequence = 0
        self._epoch = 0
        self._acquiring = False
        self._lock = threading.RLock()
        self._motion_lock = threading.Lock()
        # Connect directly to the configured LAN service.
        self._opener = build_opener(ProxyHandler({}), _NoRedirect(),
                                   DeadlineHTTPHandler(), DeadlineHTTPSHandler())

    @property
    def target(self) -> Optional[TargetTransport]:
        """Explicit motion transport, or None for the original request lifecycle."""
        return self._target

    @property
    def lease(self) -> Optional[str]:
        """Current private lease capability, or None; do not log this value."""
        with self._lock:
            return self._lease

    def _forget_locked(self) -> int:
        self._lease = None
        self._dog_action_id = None
        self._arm_operation_id = None
        self._acquiring = False
        self._epoch += 1
        return self._epoch

    def _forget(self, epoch: int) -> None:
        with self._lock:
            if epoch == self._epoch:
                self._forget_locked()

    def _observe_status(self, result: ControlStatus, epoch: int,
                        lease: Optional[str]) -> None:
        action = result['dog']['action']
        terminal = action is not None and action['phase'] in ('completed', 'cancelled', 'fault')
        if lease is not None:
            with self._lock:
                owns_action = (action is not None and self._dog_action_id is not None
                               and action['id'] == self._dog_action_id)
                ended = terminal and (not result['lease_active'] or owns_action)
                inactive = self._target is not None and not result['lease_active']
                if epoch == self._epoch and self._lease == lease and (ended or inactive):
                    self._forget_locked()

    def _request(self, action: str, validate: Callable[[object], _Response],
                 body: Optional[Mapping[str, object]] = None, epoch: Optional[int] = None,
                 timeout: Optional[float] = None, deadline: Optional[float] = None,
                 *, forget_on_error: bool = True) -> _Response:
        with self._lock:
            if epoch is None:
                epoch = self._epoch
        # Never hold the ownership mutex during network I/O: even an arm read
        # within its timeout can outlast the server's control lease.
        try:
            headers = {'Accept': 'application/json'}
            data = None
            if body is not None:
                data = json.dumps(body, allow_nan=False).encode('utf-8')
                headers['Content-Type'] = 'application/json'
            request = Request(self.url + '/api/' + action, data=data, headers=headers)
            if deadline is not None:
                setattr(request, '_lite3_deadline', deadline)
            with self._opener.open(request, timeout=self.timeout if timeout is None else timeout) as response:
                _check_protocol(response.headers.get_all(PROTOCOL_HEADER))
                raw = response.read(self.MAX_RESPONSE + 1)
            return validate(self._decode_response(raw))
        except HTTPError as error:
            if forget_on_error:
                self._forget(epoch)
            message = 'Control request rejected (HTTP {})'.format(error.code)
            code = None
            try:
                _check_protocol(error.headers.get_all(PROTOCOL_HEADER) if error.headers is not None else None)
                raw = error.read(self.MAX_RESPONSE + 1)
                envelope = _responses.error_response(self._decode_response(raw))
                message = envelope['error']
                code = envelope.get('code') or None
            except DevClientError as protocol_error:
                message = str(protocol_error)
            except (ValueError, OSError, HTTPException, RecursionError):
                pass
            finally:
                error.close()
            raise DevClientError(message, error.code, code) from None
        except (URLError, OSError, HTTPException, ValueError, RecursionError, DevClientError) as error:
            if deadline is not None and isinstance(error, OSError) and time.monotonic() >= deadline:
                raise _WaitDeadlineExpired() from None
            if forget_on_error:
                self._forget(epoch)
            if isinstance(error, DevClientError):
                raise
            raise DevClientError('Control request failed: ' + str(error)) from None

    def _decode_response(self, raw: bytes) -> object:
        if len(raw) > self.MAX_RESPONSE:
            raise ValueError('Server response is too large')
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_json_object,
                          parse_constant=_parse_json_float,
                          parse_float=_parse_json_float)

    def _owned(self, action: str, values: Optional[Mapping[str, object]] = None,
               motion: bool = False) -> ControlStatus:
        with self._lock:
            if self._lease is None:
                raise DevClientError('Acquire control explicitly before this operation')
            epoch = self._epoch
        if motion:
            try:
                _validate_parameters(cast(MotionOperation, action), values or {})
            except ValueError as error:
                raise DevClientError(str(error)) from None
            with self._motion_lock:
                return self._owned_request(action, values, epoch, motion=True)
        return self._owned_request(action, values, epoch)

    def _owned_request(self, action: str, values: Optional[Mapping[str, object]],
                       epoch: int, motion: bool = False) -> ControlStatus:
        with self._lock:
            if epoch != self._epoch or self._lease is None:
                raise DevClientError('Control operation was superseded by a session change')
            lease = self._lease
        if motion and self._target is not None:
            status = self._request('status', _responses.control_status, epoch=epoch)
            decision = check_preflight(status, cast(MotionOperation, action), self._target,
                                       owns_lease=True, parameters=values or {})
            with self._lock:
                if epoch != self._epoch or self._lease != lease:
                    raise DevClientError('Control operation was superseded by a session change')
                self._observe_status(status, epoch, lease)
            if not decision['ok']:
                raise PreflightError(decision)
        with self._lock:
            if epoch != self._epoch or self._lease != lease:
                raise DevClientError('Control operation was superseded by a session change')
            body = dict(values or {})
            body['lease'] = lease
            if motion:
                self._sequence += 1
                body['seq'] = self._sequence
        result = self._request(action, _responses.control_status, body, epoch)
        with self._lock:
            if epoch != self._epoch:
                raise DevClientError('Control operation was superseded by a session change')
            if action == 'dog/action':
                dog_action = result['dog']['action']
                if dog_action is not None:
                    self._dog_action_id = dog_action['id']
            if action == 'arm/action' or (action == 'arm/jog' and not result['arm']['simulated']):
                operation = result['arm'].get('operation')
                if operation is not None:
                    self._arm_operation_id = operation['operation_id']
            self._observe_status(result, epoch, lease)
        return result

    def status(self) -> ControlStatus:
        with self._lock:
            epoch = self._epoch
            lease = self._lease
        result = self._request('status', _responses.control_status, epoch=epoch)
        self._observe_status(result, epoch, lease)
        return result

    def preflight(self, operation: MotionOperation, *, status: Optional[ControlStatus] = None,
                  parameters: Optional[Mapping[str, object]] = None) -> PreflightResult:
        """Inspect readiness without POSTs; supplied snapshots avoid network I/O.

        An explicit constructor target is required. Automatic motion checks always
        fetch a fresh status even after a successful manual preflight.
        """
        if self._target is None:
            raise ValueError('Select a target when constructing DevClient to use preflight')
        with self._lock:
            epoch, lease = self._epoch, self._lease
        if status is None:
            status = self._request('status', _responses.control_status, epoch=epoch)
            self._observe_status(status, epoch, lease)
        with self._lock:
            owns_lease = lease is not None and self._lease == lease and self._epoch == epoch
            acquisition_busy = self._lease is not None or self._acquiring
        decision = check_preflight(status, operation, self._target,
                                   owns_lease=owns_lease if operation != 'acquire' else False,
                                   parameters=parameters)
        if operation == 'acquire' and acquisition_busy:
            if not any(reason['code'] == 'lease_busy' for reason in decision['reasons']):
                decision['reasons'].append({'code': 'lease_busy',
                                           'message': 'Release the current or pending acquisition before acquiring again'})
            decision['ok'] = False
        return decision

    def acquire(self, owner: str = 'python-sdk') -> AcquireResponse:
        with self._lock:
            if self._lease is not None or self._acquiring:
                raise DevClientError('Release the current lease before acquiring again')
            self._epoch += 1
            epoch = self._epoch
            self._acquiring = True
        body: AcquireRequest = {'owner': owner}
        try:
            if self._target is not None:
                status = self._request('status', _responses.control_status, epoch=epoch)
                decision = check_preflight(status, 'acquire', self._target)
                if not decision['ok']:
                    raise PreflightError(decision)
                with self._lock:
                    if epoch != self._epoch:
                        raise DevClientError('Control acquisition was superseded by a session change')
            result = self._request('control/acquire', _responses.acquire, body, epoch)
            response_decision = None
            if self._target is not None:
                response_decision = check_preflight(result['status'], 'acquire', self._target, owns_lease=True)
            with self._lock:
                rejection: DevClientError
                if epoch != self._epoch:
                    rejection = DevClientError('Control acquisition was superseded by a session change')
                elif response_decision is not None and not response_decision['ok']:
                    rejection = PreflightError(response_decision)
                else:
                    # Reads started during acquisition belong to the pending attempt.
                    self._epoch += 1
                    self._lease = result['lease']
                    self._sequence = 0
                    self._acquiring = False
                    return result
            if self._target is not None:
                # Release only the returned capability, outside the ownership
                # mutex, also when close/stop superseded this acquisition.
                try:
                    self._request('control/release', _responses.control_status,
                                  {'lease': result['lease']}, epoch)
                except DevClientError as cleanup_error:
                    raise rejection from cleanup_error
            raise rejection
        except DevClientError:
            self._forget(epoch)
            raise

    def heartbeat(self) -> ControlStatus:
        return self._owned('control/heartbeat')

    def stand(self) -> ControlStatus:
        """Start staged stand preparation; wait for status dog.phase == ready."""
        return self._owned('dog/stand', motion=True)

    def velocity(self, vx: float = 0.0, vy: float = 0.0, yaw: float = 0.0,
                 ttl: float = 0.3) -> ControlStatus:
        """Submit m/s, m/s, rad/s for at most ttl seconds; no automatic renewal."""
        return self._owned('dog/velocity', {'vx': vx, 'vy': vy, 'yaw': yaw, 'ttl': ttl}, motion=True)

    def dog_action(self, name: DogActionName, speed: float, duration: float) -> ControlStatus:
        """Start one timed action after explicit stand and fresh standing feedback.

        Speed is positive m/s for translation or rad/s for turns; duration is
        positive seconds, at most 10. Returns immediately with dog.action. Send
        explicit heartbeats while it runs; completion stops and revokes the lease.
        """
        return self._owned('dog/action', {'name': name, 'speed': speed, 'duration': duration}, motion=True)

    def dog_action_status(self, action_id: str) -> DogActionStatus:
        """Read an active or retained action without changing this client's lease.

        Terminal results are retained for up to 300 seconds and 128 records.
        Unknown or evicted IDs raise DevClientError with status 404.
        """
        def validate(value: object) -> DogActionStatus:
            result = _responses.dog_action_status(value)
            if result['id'] != action_id:
                raise ValueError('Response dog action ID does not match the requested ID')
            return result

        body: DogActionStatusRequest = {'id': action_id}
        return self._request('dog/action/status', validate, body, forget_on_error=False)

    def forward(self, speed: float = 0.1, duration: float = 1.0) -> ControlStatus:
        """Start forward motion at positive speed (m/s), at most 0.2 m/s."""
        return self.dog_action('forward', speed, duration)

    def backward(self, speed: float = 0.1, duration: float = 1.0) -> ControlStatus:
        """Start backward motion at positive speed (m/s), at most 0.2 m/s."""
        return self.dog_action('backward', speed, duration)

    def turn_left(self, speed: float = 0.2, duration: float = 1.0) -> ControlStatus:
        """Start a left turn at positive speed (rad/s), at most 0.3 rad/s."""
        return self.dog_action('turn_left', speed, duration)

    def turn_right(self, speed: float = 0.2, duration: float = 1.0) -> ControlStatus:
        """Start a right turn at positive speed (rad/s), at most 0.3 rad/s."""
        return self.dog_action('turn_right', speed, duration)

    def stop(self) -> ControlStatus:
        """Stop the dog, revoke ownership, and cancel preparation (observer allowed)."""
        with self._lock:
            body: StopRequest = {'lease': self._lease} if self._lease else {}
            epoch = self._forget_locked()
        return self._request('dog/stop', _responses.control_status, body, epoch)

    def release(self) -> Optional[ControlStatus]:
        return self._release()

    def _release(self, allow_completed_action: bool = False) -> Optional[ControlStatus]:
        with self._lock:
            lease = self._lease
            action_id = self._dog_action_id
            epoch = self._forget_locked()
            if lease is None:
                return None
        body: LeaseRequest = {'lease': lease}
        try:
            return self._request('control/release', _responses.control_status, body, epoch)
        except DevClientError as error:
            if not allow_completed_action or error.status != 409 or action_id is None:
                raise
            # Completion revokes this lease. A newer controller may already own
            # the service; only read back our acknowledged action to confirm it.
            result = self._request('status', _responses.control_status, epoch=epoch)
            action = result['dog']['action']
            if action is not None and action['id'] == action_id and action['phase'] == 'completed':
                return result
            raise

    def estop(self) -> ControlStatus:
        with self._lock:
            epoch = self._forget_locked()
        return self._request('control/estop', _responses.control_status, {}, epoch)

    def reset(self, revision: str) -> ControlStatus:
        """Clear only the explicitly observed status['safety_revision'] latch."""
        with self._lock:
            epoch = self._forget_locked()
        body: ResetRequest = {'revision': revision}
        return self._request('control/reset', _responses.control_status, body, epoch)

    def arm_jog(self, joint: ArmJoint, delta: float, speed: float = 2.0) -> ControlStatus:
        """Relative degrees; commissioned hardware returns an asynchronous operation.

        Hardware permits b/e/h with speed <=1; inspect status capabilities first.
        Explicit heartbeats remain necessary while an operation is active.
        """
        return self._owned('arm/jog', {'joint': joint, 'delta': delta, 'speed': speed}, motion=True)

    def arm_operation(self, operation_id: str) -> ArmOperation:
        """Read an active/last operation; this never resubmits its target."""
        body: ArmOperationRequest = {'id': operation_id}
        return self._request('arm/operation', _responses.arm_operation, body)

    def arm_action(self, targets: Mapping[ArmJoint, float], duration: float) -> ControlStatus:
        """Submit absolute joint targets once, for 0 < duration <= 10 seconds.

        Requires dev simulation and an acquired lease. Omitted joints keep their
        targets. Read arm.operation or explicitly call wait_arm_operation.
        """
        return self._owned('arm/action', {'targets': dict(targets), 'duration': duration}, motion=True)

    def wait_arm_operation(self, operation_id: str, timeout: float = 30.0,
                           poll_interval: float = .1, renew_lease: bool = True,
                           heartbeat_interval: float = .5) -> ArmOperation:
        """Poll one ID, optionally renewing the lease held at entry.

        Returns completed, cancelled, fault or commanded_unverified unchanged.
        Observers can wait without a lease. A session change ends this wait's
        renewal; it continues observing the same ID. DevClientTimeout includes
        the last snapshot. No renewal continues after this method exits. A
        timeout leaves an existing local lease available for explicit cleanup.
        Network failures follow the usual DevClientError ownership rules.
        """
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128:
            raise ValueError('operation_id must identify an arm operation')
        timeout = _positive_seconds('timeout', timeout)
        poll_interval = _positive_seconds('poll_interval', poll_interval)
        heartbeat_interval = _positive_seconds('heartbeat_interval', heartbeat_interval)
        if not isinstance(renew_lease, bool):
            raise ValueError('renew_lease must be boolean')
        if renew_lease and heartbeat_interval > 1.0:
            raise ValueError('heartbeat_interval must be at most 1 second when renewing')
        with self._lock:
            epoch = self._epoch
            lease = self._lease if renew_lease else None
        now = time.monotonic()
        deadline = now + timeout
        next_poll = next_heartbeat = now
        operation: Optional[ArmOperation] = None

        def remaining() -> float:
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise DevClientTimeout(operation_id, operation)
            return min(self.timeout, seconds)

        try:
            while True:
                remaining()
                if time.monotonic() >= next_poll:
                    body: ArmOperationRequest = {'id': operation_id}
                    candidate = self._request('arm/operation', _responses.arm_operation, body,
                                              epoch, timeout=remaining(), deadline=deadline)
                    if candidate['operation_id'] != operation_id:
                        self._forget(epoch)
                        raise DevClientError('Arm operation response identified a different operation')
                    operation = candidate
                    remaining()
                    if operation['phase'] in ('completed', 'cancelled', 'fault', 'commanded_unverified'):
                        with self._lock:
                            if (operation['phase'] == 'cancelled' and epoch == self._epoch
                                    and operation_id == self._arm_operation_id):
                                self._forget_locked()
                        return operation
                    next_poll = time.monotonic() + poll_interval
                with self._lock:
                    if epoch != self._epoch or lease != self._lease:
                        lease = None
                if lease is not None and time.monotonic() >= next_heartbeat:
                    heartbeat: LeaseRequest = {'lease': lease}
                    try:
                        result = self._request('control/heartbeat', _responses.control_status,
                                               heartbeat, epoch, timeout=remaining(), deadline=deadline)
                    except DevClientError as error:
                        if error.status != 409:
                            raise
                        # Ownership can be revoked between a poll and heartbeat.
                        # Observe the same operation without further renewal.
                        lease = None
                        next_poll = time.monotonic()
                        continue
                    self._observe_status(result, epoch, lease)
                    remaining()
                    next_heartbeat = time.monotonic() + heartbeat_interval
                wake = min(deadline, next_poll, next_heartbeat if lease is not None else deadline)
                time.sleep(max(0.0, wake - time.monotonic()))
        except _WaitDeadlineExpired:
            raise DevClientTimeout(operation_id, operation) from None

    def arm_stop(self) -> ControlStatus:
        """Cancel arm work and revoke ownership; physical stopping stays unverified."""
        with self._lock:
            body: StopRequest = {'lease': self._lease} if self._lease else {}
            epoch = self._forget_locked()
        return self._request('arm/stop', _responses.control_status, body, epoch)

    def arm_state(self) -> ArmState:
        """Sample hardware explicitly; a commissioned first read consumes its nonce.

        For passive cached feedback and commissioning state, use status()['arm'].
        """
        return self._request('arm/state', _responses.arm_state, {})

    def arm_reconnect(self) -> ArmReconnectResponse:
        """Clear the read latch; the next read opens transport, motion stays disarmed."""
        return self._request('arm/reconnect', _responses.arm_reconnect, {})

    def close(self) -> None:
        """Release ownership, accepting confirmed completion of this session's action."""
        self._release(allow_completed_action=True)

    def __enter__(self) -> 'DevClient':
        return self

    def __exit__(self, exception_type: Optional[Type[BaseException]],
                 exception: Optional[BaseException], traceback: Optional[TracebackType]) -> Literal[False]:
        try:
            self.close()
        except DevClientError:
            if exception_type is None:
                raise
        return False
