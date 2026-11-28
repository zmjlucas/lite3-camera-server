"""Timed simulated joint targets, serialized by the control service's lock."""
import copy
import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Callable, Dict, Optional
    from lite3_dev_server.api import ArmJoint, SimulatedArmOperation


MAX_DURATION = 10.0


class SimulatedArmActions:
    def __init__(self, clock: 'Callable[[], float]') -> None:
        self._clock = clock
        self._started = 0.0
        self._current: 'Optional[SimulatedArmOperation]' = None

    @property
    def running(self) -> bool:
        return self._current is not None and self._current['phase'] == 'moving'

    def start(self, positions: 'Dict[ArmJoint, float]', targets: 'Dict[ArmJoint, float]',
              duration: float) -> None:
        """Accept already validated targets while the control lock is held."""
        goal = dict(positions)
        goal.update(targets)
        self._started = self._clock()
        self._current = {
            'operation_id': secrets.token_urlsafe(12), 'kind': 'simulated_targets',
            'simulated': True, 'phase': 'moving', 'reason': None,
            'duration': duration, 'elapsed_s': 0.0, 'progress': 0.0,
            'start_position_deg': dict(positions), 'target_position_deg': goal,
            'position_deg': dict(positions), 'stop_verified': False,
        }

    def advance(self, positions: 'Dict[ArmJoint, float]', now: float) -> None:
        """Advance only after the caller has checked expiry and cancellation."""
        op = self._current
        if op is None or not self.running:
            return
        elapsed = min(op['duration'], max(0.0, now - self._started))
        progress = elapsed / op['duration']
        for joint, target in op['target_position_deg'].items():
            start = op['start_position_deg'][joint]
            positions[joint] = target if progress == 1.0 else start + (target - start) * progress
        op.update({'position_deg': dict(positions), 'elapsed_s': elapsed, 'progress': progress})
        if progress == 1.0:
            op.update({'phase': 'completed', 'reason': 'duration_elapsed'})

    def cancel(self, reason: str) -> None:
        if self._current is not None and self.running:
            self._current.update({'phase': 'cancelled', 'reason': reason})

    def status(self) -> 'Optional[SimulatedArmOperation]':
        return copy.deepcopy(self._current)
