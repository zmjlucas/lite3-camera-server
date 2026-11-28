"""JSON request and response contracts for the Lite3 control API."""
from typing import Dict, List, Literal, Optional, TypedDict, Union


Environment = Literal['dev', 'dist']
ControlMode = Literal['disabled', 'readonly', 'hardware']
DogPhase = Literal['disarmed', 'preparing', 'ready', 'moving', 'stopping', 'error']
DogActionName = Literal['forward', 'backward', 'turn_left', 'turn_right']
VelocityAxis = Literal['vx', 'vy', 'yaw']
VelocityLimit = Literal['vx', 'vy', 'yaw', 'ttl']
ArmJoint = Literal['b', 's', 'e', 'w', 'h']
ArmFeedbackJoint = Literal['b', 's', 'e', 'w']


class DogFeedback(TypedDict):
    observed: bool
    simulated: bool
    fresh: bool
    age_s: Optional[float]
    basic_state: Optional[int]
    gait_state: Optional[int]
    motion_state: Optional[int]
    standing: bool
    motion_ready: bool
    reason: Optional[str]


class DogActionStatus(TypedDict):
    id: str
    name: DogActionName
    speed: float
    duration: float
    phase: Literal['running', 'completed', 'cancelled', 'fault']
    reason: Optional[str]
    remaining_s: float


class DogStatus(TypedDict):
    phase: DogPhase
    velocity: Dict[VelocityAxis, float]
    limits: Dict[VelocityLimit, float]
    error: Optional[str]
    stop_verified: Literal[False]
    simulated: bool
    motion_enabled: bool
    feedback: DogFeedback
    action: Optional[DogActionStatus]
    actions_enabled: bool


class ArmSample(TypedDict):
    position_deg: Dict[ArmFeedbackJoint, float]
    sample_monotonic: float


class ArmOperation(TypedDict):
    operation_id: str
    phase: Literal['preflight', 'moving', 'stopping', 'completed', 'cancelled',
                   'fault', 'commanded_unverified']
    reason: Optional[str]
    joint: Literal['b', 'e', 'h']
    delta_deg: float
    speed_deg_s: float
    observed_start: Optional[ArmSample]
    observed_end: Optional[ArmSample]
    targets_written: List[Dict[ArmJoint, float]]
    hold_status: Literal['not_needed', 'requested', 'written', 'unavailable']
    observed_stationary: Optional[bool]
    stop_verified: Literal[False]
    gripper_position_deg: None


class ArmCapabilities(TypedDict):
    joints: List[Literal['b', 'e', 'h']]
    max_delta_deg: Dict[Literal['b', 'e', 'h'], float]
    max_speed_deg_s: float
    max_travel_deg: float
    asynchronous: Literal[True]
    stop_verified: Literal[False]
    gripper_feedback: Literal[False]


class _OptionalArmStatus(TypedDict, total=False):
    operation: Optional[ArmOperation]
    capabilities: ArmCapabilities


class ArmStatus(_OptionalArmStatus):
    motion_enabled: bool
    reason: Optional[str]
    position_deg: Optional[Dict[ArmJoint, float]]
    simulated: bool


class ControlStatus(TypedDict):
    mode: ControlMode
    environment: Environment
    estop: bool
    safety_revision: str
    lease_active: bool
    dog: DogStatus
    arm: ArmStatus


class DisabledStatus(TypedDict):
    """Minimal status when the HTTP server has no control service."""

    mode: Literal['disabled']


StatusResponse = Union[ControlStatus, DisabledStatus]


class AcquireResponse(TypedDict):
    lease: str
    status: ControlStatus


class _OptionalArmSample(TypedDict, total=False):
    sample_monotonic: float


class ObservedArmState(_OptionalArmSample):
    """Hardware feedback has four measured axes and an unknown gripper angle."""

    position_deg: Dict[ArmFeedbackJoint, float]
    gripper_position_deg: None
    observed: Literal[True]
    motion_enabled: bool
    reason: Optional[str]


class UnobservedArmState(ArmStatus):
    observed: Literal[False]


ArmState = Union[ObservedArmState, UnobservedArmState]


class ArmGatewayReconnect(TypedDict):
    """A reconnect clears the gateway latch and leaves the transport unopened."""

    connected: Literal[False]
    reconnect_required: Literal[False]
    motion_enabled: Literal[False]
    reason: str


ArmReconnectResponse = Union[ArmGatewayReconnect, UnobservedArmState]


class AcquireRequest(TypedDict):
    owner: str


class LeaseRequest(TypedDict):
    lease: str


class MotionRequest(LeaseRequest):
    seq: int


class StopRequest(TypedDict, total=False):
    lease: str
    seq: int


class ResetRequest(TypedDict):
    revision: str


class VelocityRequest(MotionRequest):
    vx: float
    vy: float
    yaw: float
    ttl: float


class DogActionRequest(MotionRequest):
    name: DogActionName
    speed: float
    duration: float


class ArmJogRequest(MotionRequest):
    joint: ArmJoint
    delta: float
    speed: float


class ArmOperationRequest(TypedDict):
    id: str
