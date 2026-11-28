"""Small synchronous Lite3 Dev Server SDK (Python 3.8+, standard library only).

No operation retries or acquires ownership automatically. Call heartbeat explicitly
while holding a lease. Velocity TTL is independent of that lease heartbeat.
"""
from http.client import HTTPException, HTTPMessage
import json
import math
import threading
from types import TracebackType
from typing import IO, Any, Dict, Literal, Mapping, Optional, Type, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .api import (
    AcquireRequest, AcquireResponse, ArmJoint, ArmReconnectResponse, ArmState,
    ArmOperation, ArmOperationRequest,
    ControlStatus, DogActionName, LeaseRequest, ResetRequest, StatusResponse, StopRequest,
)


class DevClientError(RuntimeError):
    """The server rejected a request, or its response could not be trusted."""

    status: Optional[int]

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: IO[bytes], code: int, msg: str,
                         headers: HTTPMessage, newurl: str) -> None:
        # Send robot commands only to the configured service.
        return None


class DevClient:
    """Explicit API calls; context exit releases an acquired lease.

    Stops, resets, terminal dog actions and failed requests forget ownership for
    their own session.
    Reacquire and explicitly prepare the dog again before further motion. A
    successful response is software state, never proof of physical robot state.
    Motion calls are serialized; reads, heartbeats and stops can run concurrently.
    Replies from an earlier ownership session cannot change the current session.
    """

    MAX_RESPONSE = 65536

    def __init__(self, url: str, timeout: float = 2.0) -> None:
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or
                parsed.query or parsed.fragment or parsed.path not in ('', '/')):
            raise ValueError('url must be an HTTP(S) server origin without credentials, query or path')
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
                not math.isfinite(timeout) or not 0 < timeout <= 10):
            raise ValueError('timeout must be finite, positive and at most 10 seconds')
        self.url = url.rstrip('/')
        self.timeout = float(timeout)
        self._lease: Optional[str] = None
        self._dog_action_id: Optional[str] = None
        self._sequence = 0
        self._epoch = 0
        self._acquiring = False
        self._lock = threading.RLock()
        self._motion_lock = threading.Lock()
        # Connect directly to the configured LAN service.
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    @property
    def lease(self) -> Optional[str]:
        """Current private lease capability, or None; do not log this value."""
        with self._lock:
            return self._lease

    def _forget_locked(self) -> int:
        self._lease = None
        self._dog_action_id = None
        self._acquiring = False
        self._epoch += 1
        return self._epoch

    def _forget(self, epoch: int) -> None:
        with self._lock:
            if epoch == self._epoch:
                self._forget_locked()

    @staticmethod
    def _action_status(result: Mapping[str, Any]) -> Mapping[str, object]:
        dog = result.get('dog')
        action = cast(Dict[str, object], dog).get('action') if isinstance(dog, dict) else None
        return cast(Dict[str, object], action) if isinstance(action, dict) else {}

    def _observe_dog_action(self, result: Mapping[str, Any], epoch: int,
                           lease: Optional[str]) -> None:
        action = self._action_status(result)
        if lease is not None and action.get('phase') in ('completed', 'cancelled', 'fault'):
            with self._lock:
                owns_action = self._dog_action_id is not None and action.get('id') == self._dog_action_id
                if (epoch == self._epoch and self._lease == lease and
                        (result.get('lease_active') is False or owns_action)):
                    self._forget_locked()

    def _request(self, action: str, body: Optional[Mapping[str, object]] = None,
                 epoch: Optional[int] = None) -> Dict[str, Any]:
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
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(self.MAX_RESPONSE + 1)
            if len(raw) > self.MAX_RESPONSE:
                raise DevClientError('Server response is too large')
            result = json.loads(raw.decode('utf-8'))
            if not isinstance(result, dict):
                raise DevClientError('Server response is not a JSON object')
            return result
        except HTTPError as error:
            self._forget(epoch)
            message = 'Control request rejected (HTTP {})'.format(error.code)
            try:
                detail = json.loads(error.read(self.MAX_RESPONSE).decode('utf-8'))
                if isinstance(detail, dict) and isinstance(detail.get('error'), str):
                    message = detail['error']
            except (ValueError, UnicodeError, OSError):
                pass
            finally:
                error.close()
            raise DevClientError(message, error.code) from None
        except (URLError, OSError, HTTPException, ValueError, UnicodeError, DevClientError) as error:
            self._forget(epoch)
            if isinstance(error, DevClientError):
                raise
            raise DevClientError('Control request failed: ' + str(error)) from None

    def _owned(self, action: str, values: Optional[Mapping[str, object]] = None,
               motion: bool = False) -> ControlStatus:
        with self._lock:
            if self._lease is None:
                raise DevClientError('Acquire control explicitly before this operation')
            epoch = self._epoch
        if motion:
            with self._motion_lock:
                return self._owned_request(action, values, epoch, motion=True)
        return self._owned_request(action, values, epoch)

    def _owned_request(self, action: str, values: Optional[Mapping[str, object]],
                       epoch: int, motion: bool = False) -> ControlStatus:
        with self._lock:
            if epoch != self._epoch or self._lease is None:
                raise DevClientError('Control operation was superseded by a session change')
            lease = self._lease
            body = dict(values or {})
            body['lease'] = lease
            if motion:
                self._sequence += 1
                body['seq'] = self._sequence
        result = self._request(action, body, epoch)
        with self._lock:
            if epoch != self._epoch:
                raise DevClientError('Control operation was superseded by a session change')
            if action == 'dog/action':
                action_id = self._action_status(result).get('id')
                if isinstance(action_id, str) and action_id:
                    self._dog_action_id = action_id
            self._observe_dog_action(result, epoch, lease)
        return cast(ControlStatus, result)

    def status(self) -> StatusResponse:
        with self._lock:
            epoch = self._epoch
            lease = self._lease
        result = self._request('status', epoch=epoch)
        self._observe_dog_action(result, epoch, lease)
        return cast(StatusResponse, result)

    def acquire(self, owner: str = 'python-sdk') -> AcquireResponse:
        with self._lock:
            if self._lease is not None or self._acquiring:
                raise DevClientError('Release the current lease before acquiring again')
            self._epoch += 1
            epoch = self._epoch
            self._acquiring = True
        body: AcquireRequest = {'owner': owner}
        result = self._request('control/acquire', body, epoch)
        if not isinstance(result.get('lease'), str) or not result['lease']:
            self._forget(epoch)
            raise DevClientError('Server did not provide a control lease')
        with self._lock:
            if epoch != self._epoch:
                raise DevClientError('Control acquisition was superseded by a session change')
            self._lease = result['lease']
            self._sequence = 0
            self._acquiring = False
            return cast(AcquireResponse, result)

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
        return cast(ControlStatus, self._request('dog/stop', body, epoch))

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
            return cast(ControlStatus, self._request('control/release', body, epoch))
        except DevClientError as error:
            if not allow_completed_action or error.status != 409 or action_id is None:
                raise
            # Completion revokes this lease. A newer controller may already own
            # the service; only read back our acknowledged action to confirm it.
            result = self._request('status', epoch=epoch)
            action = self._action_status(result)
            if action.get('id') == action_id and action.get('phase') == 'completed':
                return cast(ControlStatus, result)
            raise

    def estop(self) -> ControlStatus:
        with self._lock:
            epoch = self._forget_locked()
        return cast(ControlStatus, self._request('control/estop', {}, epoch))

    def reset(self, revision: str) -> ControlStatus:
        """Clear only the explicitly observed status['safety_revision'] latch."""
        with self._lock:
            epoch = self._forget_locked()
        body: ResetRequest = {'revision': revision}
        return cast(ControlStatus, self._request('control/reset', body, epoch))

    def arm_jog(self, joint: ArmJoint, delta: float, speed: float = 2.0) -> ControlStatus:
        """Relative degrees; commissioned hardware returns an asynchronous operation.

        Hardware permits b/e/h with speed <=1; inspect status capabilities first.
        Explicit heartbeats remain necessary while an operation is active.
        """
        return self._owned('arm/jog', {'joint': joint, 'delta': delta, 'speed': speed}, motion=True)

    def arm_operation(self, operation_id: str) -> ArmOperation:
        """Read an active/last operation; this never resubmits its target."""
        body: ArmOperationRequest = {'id': operation_id}
        return cast(ArmOperation, self._request('arm/operation', body))

    def arm_stop(self) -> ControlStatus:
        """Cancel arm work and revoke ownership; physical stopping stays unverified."""
        with self._lock:
            body: StopRequest = {'lease': self._lease} if self._lease else {}
            epoch = self._forget_locked()
        return cast(ControlStatus, self._request('arm/stop', body, epoch))

    def arm_state(self) -> ArmState:
        return cast(ArmState, self._request('arm/state', {}))

    def arm_reconnect(self) -> ArmReconnectResponse:
        """Clear the read latch; the next read opens transport, motion stays disarmed."""
        return cast(ArmReconnectResponse, self._request('arm/reconnect', {}))

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
