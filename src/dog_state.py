"""Passive Lite3 posture feedback with bounded freshness and independent UDP IO.

The 208-byte native RobotState and 12-byte packed wrapper follow the vendor:
https://github.com/DeepRoboticsLab/Lite3_ROS/blob/e4ab5dc1c7f3ff46274162a1ef302253936578aa/src/transfer/include/protocol.hpp#L29-L55
The inner structure aligns doubles to 8 bytes; data starts at packet byte 12.
Older 200-byte layouts are rejected. This receiver never transmits packets.
"""
import errno
import ipaddress
import math
import socket
import struct
import threading
import time


STATE_CODE = 0x0901
STATE_SIZE = 208
MAX_AGE = 0.5
RECEIVE_TIMEOUT = 0.1
DRAIN_BATCH = 64


def _decode(packet):
    """Decode the explicitly supported wire layout, preserving its C padding."""
    if len(packet) != 12 + STATE_SIZE:
        raise ValueError('Invalid dog state packet length')
    code, size, kind = struct.unpack_from('<III', packet)
    if code != STATE_CODE or size != STATE_SIZE or kind != 1:
        raise ValueError('Invalid dog state packet header')
    payload = memoryview(packet)[12:]
    basic, gait, policy = struct.unpack_from('<iii', payload)
    motion = struct.unpack_from('<i', payload, 172)[0]
    need_move = payload[188]
    if min(basic, gait, policy, motion) < 0 or need_move not in (0, 1):
        raise ValueError('Invalid dog state fields')
    values = (struct.unpack_from('<18d', payload, 16)
              + struct.unpack_from('<d', payload, 176)
              + struct.unpack_from('<2d', payload, 192))
    if not all(math.isfinite(value) for value in values):
        raise ValueError('Nonfinite dog state telemetry')
    return {'basic_state': basic, 'gait_state': gait, 'motion_state': motion,
            'balanced': need_move == 0}


class DogStateReceiver:
    """Own a telemetry port exclusively and expose cached feedback without IO.

    ``start`` opens the socket once. Binding or receiving failures leave motion
    blocked with a reason; ``close`` permanently clears the observed sample.
    The lifecycle mutex is separate from the short snapshot/state mutex.
    Feedback age uses the last proven empty UDP queue as an arrival lower bound,
    so queue and processing delays count toward age even before a long stall.
    """

    def __init__(self, host, port=43897, clock=time.monotonic):
        try:
            address = ipaddress.IPv4Address(host) if isinstance(host, str) else None
        except ipaddress.AddressValueError:
            address = None
        if address is None or address.is_unspecified or address.is_multicast:
            raise ValueError('Dog state host must be a unicast numeric IPv4 address')
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('Dog state port must be an integer from 1 to 65535')
        self._host = str(address)
        self._port = port
        self._clock = clock
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stop = threading.Event()
        self._socket = None
        self._thread = None
        self._started = False
        self._closed = False
        self._sample = None
        self._received_at = None
        self._initial_empty_at = None
        self._reason = 'Dog state receiver has not started'

    def start(self):
        with self._lifecycle_lock:
            if self._started or self._closed:
                return self
            self._started = True
            candidate = None
            try:
                candidate = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                candidate.settimeout(RECEIVE_TIMEOUT)
                # Exclusive ownership prevents stealing packets from a ROS receiver.
                self._initial_empty_at = self._clock()
                candidate.bind(('0.0.0.0', self._port))
            except OSError as error:
                if candidate is not None:
                    candidate.close()
                self._invalidate('Dog state receiver could not bind UDP port {}: {}'.format(
                    self._port, error))
                return self
            self._socket = candidate
            self._invalidate('Waiting for dog state feedback')
            self._thread = threading.Thread(target=self._run, name='lite3-dog-state')
            self._thread.daemon = True
            try:
                self._thread.start()
            except RuntimeError as error:
                self._thread = None
                candidate.close()
                self._invalidate('Dog state receiver failed to start: {}'.format(error))
        return self

    def close(self):
        with self._lifecycle_lock:
            with self._lock:
                self._closed = True
                self._sample = None
                self._received_at = None
                self._reason = 'Dog state receiver is closed'
            self._stop.set()
            worker = self._thread
            candidate = self._socket
            if candidate is not None:
                candidate.close()
        if worker is not None and worker is not threading.current_thread():
            worker.join(0.5)

    def snapshot(self):
        with self._lock:
            sample = self._sample
            age = None if sample is None else max(0.0, self._clock() - self._received_at)
            fresh = sample is not None and age <= MAX_AGE and not self._closed
            ready = bool(fresh and sample['basic_state'] == 6
                         and sample['gait_state'] == 0
                         and sample['motion_state'] in (0, 1) and sample['balanced'])
            standing = bool(ready and sample['motion_state'] == 0)
            reason = self._reason
            if sample is not None:
                if not fresh:
                    reason = 'Dog state feedback is stale'
                elif not ready:
                    reason = 'Dog must be balanced in the low-speed standing or walking state'
            return {
                'observed': sample is not None,
                'simulated': False,
                'fresh': fresh,
                'age_s': age,
                'basic_state': sample['basic_state'] if sample else None,
                'gait_state': sample['gait_state'] if sample else None,
                'motion_state': sample['motion_state'] if sample else None,
                'standing': standing,
                'motion_ready': ready,
                'reason': reason,
            }

    def _invalidate(self, reason):
        with self._lock:
            if self._closed:
                return
            self._sample = None
            self._received_at = None
            self._reason = reason

    def _accept_datagram(self, packet, source, received_at=None):
        if received_at is None:
            received_at = self._clock()
        if source[0] != self._host:
            return
        # Other vendor channels carry joint/IMU data and cannot refresh posture.
        if len(packet) >= 4 and struct.unpack_from('<I', packet)[0] != STATE_CODE:
            return
        try:
            sample = _decode(packet)
        except (ValueError, struct.error):
            self._invalidate('Invalid dog state feedback received')
            return
        with self._lock:
            if self._closed:
                return
            self._sample = sample
            self._received_at = received_at
            self._reason = None

    def _drain_queue(self, candidate, received_at):
        """Share one freshness bound across a backlog until the queue is empty.

        A None bound discards the backlog. Once a bound expires, all remaining
        queued packets are discarded. The returned time precedes the empty
        read and is a lower bound for the next packet's arrival.
        """
        candidate.settimeout(0.0)
        try:
            while not self._stop.is_set():
                for _ in range(DRAIN_BATCH):
                    if self._stop.is_set():
                        return None
                    checked_at = self._clock()
                    if received_at is not None and checked_at - received_at > MAX_AGE:
                        self._invalidate('Dog state backlog is stale; waiting for fresh feedback')
                        received_at = None
                    try:
                        packet, source = candidate.recvfrom(12 + STATE_SIZE + 1)
                    except BlockingIOError as error:
                        if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                            return checked_at
                        raise
                    if received_at is not None:
                        self._accept_datagram(packet, source, received_at=received_at)
                # A flooded socket stays untrusted and yields between bounded batches.
                self._stop.wait(0.001)
            return None
        finally:
            try:
                candidate.settimeout(RECEIVE_TIMEOUT)
            except OSError:
                if not self._stop.is_set():
                    raise

    def _run(self):
        candidate = self._socket
        empty_at = (self._clock() if self._initial_empty_at is None
                    else self._initial_empty_at)
        try:
            while not self._stop.is_set():
                try:
                    wait_started = self._clock()
                    try:
                        # One extra byte makes oversized datagrams fail length checks.
                        packet, source = candidate.recvfrom(12 + STATE_SIZE + 1)
                    except socket.timeout:
                        # A timeout proves emptiness; retain the pre-wait timestamp
                        # in case the thread was delayed after the timeout occurred.
                        empty_at = wait_started
                        continue
                    received_at = empty_at
                    if self._clock() - empty_at > MAX_AGE:
                        self._invalidate('Dog state receiver stalled; waiting for fresh feedback')
                        received_at = None
                    else:
                        # Dequeue time can follow arrival by an arbitrary delay.
                        # Only a previously observed empty queue bounds arrival.
                        self._accept_datagram(packet, source, received_at=received_at)
                    empty_at = self._drain_queue(candidate, received_at)
                    if empty_at is None:
                        break
                except OSError as error:
                    if not self._stop.is_set():
                        self._invalidate('Dog state receiver failed: {}'.format(error))
                    break
        finally:
            candidate.close()
