"""Bounded action parameters and progress, owned by the control writer's lock.

Native yaw sign follows DeepRoboticsLab/Lite3_ROS at e4ab5dc1c7f3ff46274162a1ef302253936578aa,
src/transfer/src/Jetson2Motion.cpp: CmdVelCallback negates ROS angular.z.
"""
from collections import OrderedDict
import math
import secrets


ACTION_AXES = {'forward': ('vx', 1), 'backward': ('vx', -1),
               'turn_left': ('yaw', -1), 'turn_right': ('yaw', 1)}
MAX_DURATION = 10.0
MAX_TERMINAL_ACTIONS = 128
TERMINAL_RETENTION_SECONDS = 300.0


def action_velocity(name, speed, duration, limits):
    if not isinstance(name, str) or name not in ACTION_AXES:
        raise ValueError('Unsupported dog action')
    axis, sign = ACTION_AXES[name]
    for field, value, maximum in (('speed', speed, limits[axis]),
                                  ('duration', duration, MAX_DURATION)):
        if (isinstance(value, bool) or not isinstance(value, (float, int))
                or not 0 < value <= maximum or not math.isfinite(value)):
            raise ValueError('{} must be greater than zero and at most {}'.format(field, maximum))
    velocity = dict(vx=0.0, vy=0.0, yaw=0.0)
    velocity[axis] = sign * float(speed)
    return velocity


class DogActions:
    def __init__(self, clock):
        self._clock = clock
        self._current = None
        self._terminal = OrderedDict()
        self.deadline = None

    @property
    def running(self):
        return self._current is not None and self._current['phase'] == 'running'

    def start(self, name, speed, duration):
        self.prune()
        self.deadline = None
        self._current = dict(id=secrets.token_urlsafe(12), name=name, speed=float(speed),
                             duration=float(duration), phase='running', reason=None)

    def mark_started(self):
        if self.running and self.deadline is None:
            self.deadline = self._clock() + self._current['duration']

    def finish(self, phase, reason):
        if self.running:
            self._current.update(phase=phase, reason=reason)
            self.deadline = None
            self._terminal[self._current['id']] = (self._clock(), self.status())
        self.prune()

    def prune(self):
        now = self._clock()
        while self._terminal:
            finished_at, _ = next(iter(self._terminal.values()))
            if (len(self._terminal) <= MAX_TERMINAL_ACTIONS
                    and now - finished_at < TERMINAL_RETENTION_SECONDS):
                break
            self._terminal.popitem(last=False)

    def lookup(self, action_id):
        """Return a copy without advancing motion or extending retention."""
        self.prune()
        if self.running and self._current['id'] == action_id:
            return self.status()
        return dict(self._terminal[action_id][1])

    def status(self):
        if self._current is None:
            return None
        result = dict(self._current)
        result['remaining_s'] = (self._current['duration'] if self.deadline is None
                                 else max(0.0, self.deadline - self._clock())) if self.running else 0.0
        return result


def feedback_status(simulate, phase, provider):
    if not simulate and provider is not None:
        return provider.snapshot()
    ready = simulate and phase in ('ready', 'moving')
    return dict(observed=False, simulated=simulate, fresh=simulate,
                age_s=0.0 if simulate else None,
                basic_state=6 if ready else None, gait_state=0 if ready else None,
                motion_state=(1 if phase == 'moving' else 0) if ready else None,
                standing=simulate and phase == 'ready', motion_ready=ready,
                reason=(None if ready else 'Explicit dog preparation is required') if simulate
                else 'Dog state feedback is unavailable')
