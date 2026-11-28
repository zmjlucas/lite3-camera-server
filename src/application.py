"""Coordinate control operations and arm observations independently of HTTP."""
from typing import TYPE_CHECKING

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
        return self.control.status()

    def dispatch(self, action: str, body: 'Any') -> 'ControlResult':
        if not isinstance(body, dict):
            raise ControlError("invalid_request", "Expected a JSON object")
        if action not in ("arm/state", "arm/reconnect"):
            return self.control.dispatch(action, body)
        if body:
            raise ControlError("invalid_request", "This operation takes an empty object")

        if self.arm_gateway is None:
            status = self.control.status()
            if status["mode"] == "disabled":
                raise ControlError("conflict", "Control is disabled")
            arm = status["arm"]
            state: 'UnobservedArmState' = {
                "observed": False, "motion_enabled": arm["motion_enabled"], "reason": arm["reason"],
                "position_deg": arm["position_deg"], "simulated": arm["simulated"],
            }
            if "operation" in arm:
                state["operation"] = arm["operation"]
            if "capabilities" in arm:
                state["capabilities"] = arm["capabilities"]
            return state

        gateway = self.arm_gateway
        result: 'Union[ObservedArmState, ArmGatewayReconnect]'
        try:
            result = gateway.read_state() if action == "arm/state" else gateway.reconnect()
        except (OSError, RuntimeError) as error:
            raise ControlError("unavailable", str(error)) from error
        # A stop can arrive during serial I/O. Take a fresh control snapshot afterwards.
        current = self.control.status()
        if not current["arm"]["motion_enabled"]:
            result["motion_enabled"] = False
        if current["estop"]:
            result["reason"] = "Software emergency stop is latched."
        return result
