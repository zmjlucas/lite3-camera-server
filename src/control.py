"""Lease-based Lite3 control with one bounded, independently scheduled UDP writer.

No socket is opened in simulation. Hardware UDP has no acknowledgement, so neither
successful preparation nor successful stop writes establish a physical state.
The R5 hardware arm is intentionally unavailable for motion in this adapter.
"""
import ipaddress
import math
import secrets
import socket
import struct
import threading
import time


HEARTBEAT = 0x21040001
VOICE = 0x21010C0A
STATIONARY = 0x21010D05
MOVE = 0x21010D06
LOW_GAIT = 0x21010300
AUTONOMOUS = 0x21010C03
AXES = (('vx', 0x0140), ('vy', 0x0145), ('yaw', 0x0141))
LIMITS = {'vx': 0.2, 'vy': 0.1, 'yaw': 0.3, 'ttl': 0.5}
JOINT_LIMITS = {'b': 180.0, 's': 90.0, 'e': 90.0, 'w': 90.0, 'h': 45.0}
ARM_BLOCK_REASON = ('Hardware arm motion blocked by 2026-11-27 R5 elbow drift '
                    'and failed hold evidence; physical stop is unverified.')
PERIOD = 0.05
LEASE_SECONDS = 2.0
SEND_TIMEOUT = 0.01
STOP_BURSTS = 10


def _command(code, value=0):
    return struct.pack('<III', code, value, 0)


def _velocity(code, value):
    return struct.pack('<IIId', code, 8, 1, value)


class ControlError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class ControlService:
    """In-memory ownership, simulation, and conservative hardware dog commands.

    ``start`` runs the watchdog independently of request/camera/arm threads.
    A short private mutex serializes state changes and individual UDP writes;
    every write has a 10 ms socket timeout. No external subsystem uses it.
    Every stop revokes the lease, invalidating even delayed high-sequence work.
    """

    def __init__(self, mode='simulation', dog_host='192.168.1.120',
                 dog_port=43893, clock=time.monotonic):
        if mode not in ('disabled', 'simulation', 'hardware'):
            raise ValueError('Unsupported control mode')
        if mode == 'hardware':
            if not isinstance(dog_host, str):
                raise ValueError('dog_host must be a numeric IPv4 address')
            try:
                address = ipaddress.ip_address(dog_host)
            except (ValueError, TypeError):
                raise ValueError('dog_host must be a numeric IPv4 address')
            if address.version != 4 or address.is_unspecified or address.is_multicast:
                raise ValueError('dog_host must be a unicast IPv4 address')
            if type(dog_port) is not int or not 1 <= dog_port <= 65535:
                raise ValueError('dog_port must be an integer from 1 to 65535')
        self.mode = mode
        self._target = (dog_host, dog_port)
        self._clock = clock
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread = None
        self._socket = None
        self._closed = False
        self._estop = False
        self._error = None
        self._lease = None
        self._lease_deadline = 0.0
        self._sequence = 0
        self._generation = 0
        self._phase = 'disarmed'
        self._velocity = dict((name, 0.0) for name, _ in AXES)
        self._velocity_deadline = None
        self._positions = dict((name, 0.0) for name in JOINT_LIMITS)
        self._stage = 0
        self._stage_deadline = 0.0
        self._next_cycle = 0.0
        self._stop_remaining = 0
        self._next_stop = 0.0

    def start(self):
        with self._lock:
            if self._closed:
                raise ControlError(503, 'Control service is closed')
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='lite3-dog-writer')
                self._thread.daemon = True
                self._thread.start()
        return self

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._stop_locked()
            worker = self._thread
            if worker is None:
                # No production writer ever ran: there is no transport to stop.
                self._stop_remaining = 0
                self._phase = 'error' if self._error else 'disarmed'
                if self._socket is not None:
                    self._socket.close()
                    self._socket = None
        self._wake.set()
        if worker is not None and worker is not threading.current_thread():
            worker.join(2.0)
            if worker.is_alive():
                with self._lock:
                    self._error = 'UDP worker shutdown timed out; physical stop is unverified'

    def status(self):
        with self._lock:
            self._expire_locked()
            return self._status_locked()

    def _status_locked(self):
        simulated = self.mode == 'simulation'
        return {
            'mode': self.mode,
            'estop': self._estop,
            'lease_active': self._lease is not None,
            'dog': {
                'phase': self._phase,
                'velocity': dict(self._velocity),
                'limits': dict(LIMITS),
                'error': self._error,
                'stop_verified': False,
                'simulated': simulated,
            },
            'arm': {
                'motion_enabled': simulated and not self._estop and not self._closed,
                'reason': ('Simulated joint targets only; no hardware feedback.'
                           if simulated else ARM_BLOCK_REASON),
                'position_deg': dict(self._positions) if simulated else None,
                'simulated': simulated,
            },
        }

    @staticmethod
    def _fields(body, allowed, required=()):
        if not isinstance(body, dict):
            raise ControlError(400, 'Expected a JSON object')
        if set(body) - set(allowed) or set(required) - set(body):
            raise ControlError(400, 'Unexpected or missing request fields')

    @staticmethod
    def _number(body, name, low, high):
        value = body.get(name)
        if (isinstance(value, bool) or not isinstance(value, (float, int))
                or not low <= value <= high or not math.isfinite(value)):
            raise ControlError(400, '{} is outside the permitted range'.format(name))
        return float(value)

    def _owner_locked(self, body):
        if (self._lease is None or not isinstance(body.get('lease'), str)
                or not secrets.compare_digest(body['lease'].encode('ascii', 'replace'),
                                              self._lease.encode('ascii'))):
            raise ControlError(409, 'A current control lease is required')

    def _motion_locked(self, body):
        self._owner_locked(body)
        sequence = body.get('seq')
        if type(sequence) is not int or sequence < 1:
            raise ControlError(400, 'seq must be a positive integer')
        if sequence <= self._sequence:
            raise ControlError(409, 'Motion sequence has already been superseded')
        if self._estop or self._error:
            raise ControlError(423, 'Control is latched; explicit reset is required')
        return sequence

    def dispatch(self, action, body):
        """Dispatch an allowlisted operation after the HTTP layer authenticates it."""
        with self._lock:
            self._expire_locked()
            if self._closed or self.mode == 'disabled':
                raise ControlError(503, 'Control service is unavailable')
            self._fields(body, body.keys() if isinstance(body, dict) else ())
            if action == 'control/acquire':
                self._fields(body, ('owner',), ('owner',))
                owner = body['owner']
                if (not isinstance(owner, str) or not owner.strip() or len(owner) > 80
                        or any(ord(char) < 32 for char in owner)):
                    raise ControlError(400, 'owner must be a short nonempty label')
                if self._estop or self._error:
                    raise ControlError(423, 'Control is latched; explicit reset is required')
                if self._lease is not None or self._stop_remaining:
                    raise ControlError(409, 'Control is owned or a stop is still in progress')
                self._lease = secrets.token_urlsafe(32)
                self._lease_deadline = self._clock() + LEASE_SECONDS
                self._sequence = 0
                self._wake.set()
                return {'lease': self._lease, 'status': self._status_locked()}
            if action in ('control/heartbeat', 'control/release'):
                self._fields(body, ('lease',), ('lease',))
                self._owner_locked(body)
                if action == 'control/heartbeat':
                    self._lease_deadline = self._clock() + LEASE_SECONDS
                else:
                    self._stop_locked()
            elif action in ('dog/stop', 'control/estop', 'control/reset'):
                self._fields(body, ('lease', 'seq'))
                if action == 'control/reset':
                    if self._stop_remaining:
                        raise ControlError(409, 'Wait for the bounded stop attempt before resetting')
                    self._error = None
                    self._estop = False
                    # Reset does not renew or recover a controller lease.
                    if self._lease is not None:
                        self._stop_locked()
                    else:
                        self._phase = 'disarmed'
                else:
                    if action == 'control/estop':
                        self._estop = True
                    self._stop_locked()
            elif action == 'dog/stand':
                self._fields(body, ('lease', 'seq'), ('lease', 'seq'))
                sequence = self._motion_locked(body)
                if self._phase != 'disarmed' or self._stop_remaining:
                    raise ControlError(409, 'Stop and acquire again before preparing')
                self._sequence = sequence
                self._generation += 1
                self._phase = 'preparing'
                self._stage = 0
                self._stage_deadline = self._clock()
                self._next_cycle = self._clock()
            elif action == 'dog/velocity':
                self._fields(body, ('lease', 'seq', 'vx', 'vy', 'yaw', 'ttl'),
                             ('lease', 'seq', 'vx', 'vy', 'yaw', 'ttl'))
                sequence = self._motion_locked(body)
                values = dict((name, self._number(body, name, -LIMITS[name], LIMITS[name]))
                              for name, _ in AXES)
                ttl = self._number(body, 'ttl', 0.0, LIMITS['ttl'])
                if ttl <= 0:
                    raise ControlError(400, 'ttl must be greater than zero')
                if self._phase not in ('ready', 'moving'):
                    raise ControlError(409, 'Explicit dog preparation is required')
                self._sequence = sequence
                self._generation += 1
                self._velocity = values
                self._velocity_deadline = self._clock() + ttl
                self._phase = 'moving'
            elif action == 'arm/jog':
                # Capability check precedes lease handling and any possible IO.
                if self.mode != 'simulation':
                    raise ControlError(423, ARM_BLOCK_REASON)
                self._fields(body, ('lease', 'seq', 'joint', 'delta', 'speed'),
                             ('lease', 'seq', 'joint', 'delta', 'speed'))
                sequence = self._motion_locked(body)
                joint = body['joint']
                if not isinstance(joint, str) or joint not in JOINT_LIMITS:
                    raise ControlError(400, 'Unsupported arm joint')
                delta = self._number(body, 'delta', -2.0, 2.0)
                speed = self._number(body, 'speed', 0.0, 5.0)
                if speed <= 0:
                    raise ControlError(400, 'speed must be greater than zero')
                target = self._positions[joint] + delta
                if abs(target) > JOINT_LIMITS[joint]:
                    raise ControlError(400, 'Simulated joint target exceeds its limit')
                self._sequence = sequence
                self._positions[joint] = target
            else:
                raise ControlError(404, 'Unknown control action')
            self._wake.set()
            return self._status_locked()

    def _stop_locked(self):
        self._generation += 1
        self._lease = None
        self._lease_deadline = 0.0
        self._velocity = dict((name, 0.0) for name, _ in AXES)
        self._velocity_deadline = None
        self._stage = 0
        if self.mode == 'hardware':
            self._stop_remaining = STOP_BURSTS
            self._next_stop = self._clock()
            self._phase = 'stopping'
        else:
            self._phase = 'error' if self._error else 'disarmed'
        self._wake.set()

    def _expire_locked(self):
        now = self._clock()
        if ((self._lease is not None and now >= self._lease_deadline)
                or (self._velocity_deadline is not None and now >= self._velocity_deadline)):
            self._stop_locked()

    def _failure_locked(self):
        self._error = 'Dog UDP send failed; physical stop is unverified'
        if not self._stop_remaining:
            self._stop_locked()

    def _send(self, packet, generation=None):
        """Only the writer calls this; guarded writes cannot outlive their lease."""
        with self._lock:
            if generation is not None:
                self._expire_locked()
                if (generation != self._generation or self._lease is None
                        or self._estop or self._error or self._closed):
                    return False
            if self.mode != 'hardware':
                return True
            try:
                if self._socket is None:
                    candidate = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    try:
                        candidate.settimeout(SEND_TIMEOUT)
                    except OSError:
                        candidate.close()
                        raise
                    self._socket = candidate
                if self._socket.sendto(packet, self._target) != len(packet):
                    raise OSError('Incomplete UDP datagram')
            except OSError:
                self._failure_locked()
                return False
            return True

    def _tick(self):
        """One scheduled writer iteration, with fresh guards before every packet."""
        with self._lock:
            self._expire_locked()
            now = self._clock()
            generation = self._generation
            stopping = self._stop_remaining > 0
            if stopping:
                if now < self._next_stop:
                    return
                self._next_stop = now + PERIOD
            else:
                if self._phase not in ('preparing', 'ready', 'moving') or now < self._next_cycle:
                    return
                self._next_cycle = now + PERIOD
        if stopping:
            # Failure of one datagram must not skip any other axis or stationary.
            for packet in (_command(HEARTBEAT), _velocity(0x0140, 0.0),
                           _velocity(0x0145, 0.0), _velocity(0x0141, 0.0),
                           _command(STATIONARY)):
                self._send(packet)
            with self._lock:
                if generation == self._generation:
                    self._stop_remaining -= 1
                    if not self._stop_remaining:
                        self._phase = 'error' if self._error else 'disarmed'
            return
        if not self._send(_command(HEARTBEAT), generation):
            return
        with self._lock:
            if generation != self._generation:
                return
            stage = self._stage if self._phase == 'preparing' else None
            due = self._clock() >= self._stage_deadline
        if stage is not None and due:
            stages = (
                ((_command(STATIONARY), _command(VOICE, 1)), 5.0),
                ((_command(MOVE),), 0.2),
                ((_command(LOW_GAIT),), 0.2),
                ((_command(AUTONOMOUS),), 0.2),
                ((), 0.0),
            )
            packets, delay = stages[stage]
            for packet in packets:
                if not self._send(packet, generation):
                    return
            with self._lock:
                self._expire_locked()
                if generation != self._generation:
                    return
                if stage == 4:
                    self._phase = 'ready'
                else:
                    self._stage += 1
                    self._stage_deadline = self._clock() + delay
        for name, code in AXES:
            with self._lock:
                value = self._velocity[name]
            if not self._send(_velocity(code, value), generation):
                return

    def _run(self):
        try:
            while True:
                self._tick()
                with self._lock:
                    if self._closed and not self._stop_remaining:
                        break
                self._wake.wait(PERIOD)
                self._wake.clear()
        finally:
            with self._lock:
                if self._socket is not None:
                    self._socket.close()
                    self._socket = None
