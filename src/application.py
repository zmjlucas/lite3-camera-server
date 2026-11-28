"""Coordinate control operations and arm observations independently of HTTP."""
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING or not __package__:
    from control import ControlError
else:
    from .control import ControlError

if TYPE_CHECKING:
    from typing import Any, Optional, Union
    from arm_gateway import ArmGateway
    from arm_motion import ArmMotionGateway
    from control import ControlService
    from lite3_dev_server.api import (
        AcquireResponse, ArmGatewayReconnect, ArmOperation, ArmReconnectResponse, ArmState,
        ControlStatus, DogActionStatus, ObservedArmState, UnobservedArmState,
    )

    ControlResult = Union[AcquireResponse, ControlStatus, ArmState, ArmReconnectResponse, ArmOperation, DogActionStatus]


class ControlApplication:
    def __init__(self, control: 'ControlService',
                 arm_gateway: 'Optional[Union[ArmGateway, ArmMotionGateway]]' = None) -> None:
        self.control = control
        self.arm_gateway = arm_gateway

    def status(self) -> 'ControlStatus':
        return self._with_arm_observation(self.control.status())

    def _with_arm_observation(self, status: 'ControlStatus') -> 'ControlStatus':
        # Commissioned snapshots are already taken by the motion owner. The
        # read-only gateway belongs to this application and supplies observations only.
        if self.arm_gateway is not None and not self.control.arm_diagnostics:
            observation = self.arm_gateway.status()
            status["arm"].update({
                "connected": observation["connected"],
                "reconnect_required": observation["reconnect_required"],
                "commissioning": observation["commissioning"],
                "cached_feedback": observation["cached_feedback"],
            })
        return status

    def snapshot(self) -> 'ControlStatus':
        return self._with_arm_observation(self.control.snapshot())

    def dispatch(self, action: str, body: 'Any') -> 'ControlResult':
        if not isinstance(body, dict):
            raise ControlError("invalid_request", "Expected a JSON object")
        if action not in ("arm/state", "arm/reconnect"):
            result = self.control.dispatch(action, body)
            if "status" in result:
                acquired = cast('AcquireResponse', result)
                self._with_arm_observation(acquired["status"])
            elif "arm" in result:
                self._with_arm_observation(cast('ControlStatus', result))
            return result
        if body:
            raise ControlError("invalid_request", "This operation takes an empty object")

        if self.arm_gateway is None:
            status = self.control.status()
            if status["mode"] == "disabled":
                raise ControlError("conflict", "Control is disabled")
            arm = status["arm"]
            return cast('UnobservedArmState', dict(arm, observed=False))

        gateway = self.arm_gateway
        observation: 'Union[ObservedArmState, ArmGatewayReconnect]'
        try:
            observation = gateway.read_state() if action == "arm/state" else gateway.reconnect()
        except (OSError, RuntimeError) as error:
            raise ControlError("unavailable", str(error)) from error
        # A stop can arrive during serial I/O. Take a fresh control snapshot afterwards.
        current = self.control.status()
        if not current["arm"]["motion_enabled"]:
            observation["motion_enabled"] = False
        if current["estop"]:
            observation["reason"] = "Software emergency stop is latched."
        return observation
