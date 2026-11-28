"""JSON request and response contracts for the Lite3 control and camera APIs."""
from typing import Dict, List, Literal, Optional, TypedDict, Union


# Every API response carries exactly one protocol version header.
PROTOCOL_HEADER = 'X-Lite3-Dev-Protocol'
PROTOCOL_VERSION = '1'
CAMERA_PROTOCOL_HEADER = 'X-Lite3-Camera-Protocol'
CAMERA_PROTOCOL_VERSION = '1'

CameraBackend = Literal['realsense', 'v4l2', 'rtsp', 'none']
CameraHealthStatus = Literal['starting', 'live', 'stale', 'error']


class CameraSource(TypedDict):
    id: str
    label: str
    device: str
    backend: CameraBackend
    available: bool
    error: Optional[str]


class CameraSourceCatalog(TypedDict):
    sources: List[CameraSource]
    selected: Optional[str]


class CameraHealth(TypedDict):
    source: CameraBackend
    source_id: Optional[str]
    status: CameraHealthStatus
    frame_age_s: Optional[float]
    error: Optional[str]
    generation: int


class _OptionalErrorCode(TypedDict, total=False):
    code: str


class ErrorResponse(_OptionalErrorCode):
    """HTTP error envelope. A semantic code, when present, is stable across messages."""

    error: str


Environment = Literal['dev', 'dist']
ControlMode = Literal['disabled', 'readonly', 'hardware']
TargetTransport = Literal['simulation', 'hardware']
MotionOperation = Literal['acquire', 'dog/stand', 'dog/velocity', 'dog/action', 'arm/jog', 'arm/action']
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


class DogActionCapability(TypedDict):
    axis: Literal['vx', 'yaw']
    sign: Literal[-1, 1]
    max_speed: float
    speed_unit: Literal['m/s', 'rad/s']


class DogCapabilities(TypedDict):
    actions: Dict[DogActionName, DogActionCapability]
    max_duration: float


class _OptionalDogStatus(TypedDict, total=False):
    capabilities: DogCapabilities


class DogStatus(_OptionalDogStatus):
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


class ArmCachedFeedback(ArmSample):
    """Last observation; times use the gateway host's monotonic clock."""

    source: Literal['local_serial', 'ssh_serial']
    age_s: float
    fresh: bool
    observed: Literal[True]
    gripper_position_deg: None


class ArmCommissioning(TypedDict):
    state: Literal['unavailable', 'simulated', 'readonly', 'pending_preflight',
                   'ready', 'recommission_required', 'closed']
    profile_loaded: bool
    nonce_consumed: bool


class ArmObservationStatus(TypedDict):
    connected: bool
    reconnect_required: bool
    commissioning: ArmCommissioning
    cached_feedback: Optional[ArmCachedFeedback]


class ArmDiagnosticOperation(TypedDict):
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


class SimulatedArmOperation(TypedDict):
    operation_id: str
    kind: Literal['simulated_targets']
    simulated: Literal[True]
    phase: Literal['moving', 'completed', 'cancelled', 'fault']
    reason: Optional[str]
    duration: float
    elapsed_s: float
    progress: float
    start_position_deg: Dict[ArmJoint, float]
    target_position_deg: Dict[ArmJoint, float]
    position_deg: Dict[ArmJoint, float]
    stop_verified: Literal[False]


ArmOperation = Union[ArmDiagnosticOperation, SimulatedArmOperation]


class ArmCapabilities(TypedDict):
    joints: List[Literal['b', 'e', 'h']]
    max_delta_deg: Dict[Literal['b', 'e', 'h'], float]
    max_speed_deg_s: float
    max_travel_deg: float
    asynchronous: Literal[True]
    stop_verified: Literal[False]
    gripper_feedback: Literal[False]


class _OptionalArmStatus(TypedDict, total=False):
    # Additions to protocol v1; older servers may omit these fields.
    connected: bool
    reconnect_required: bool
    commissioning: ArmCommissioning
    cached_feedback: Optional[ArmCachedFeedback]
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


class PreflightReason(TypedDict):
    code: str
    message: str


class PreflightResult(TypedDict):
    ok: bool
    operation: MotionOperation
    target: TargetTransport
    reasons: List[PreflightReason]
    status: ControlStatus


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


class DogActionStatusRequest(TypedDict):
    id: str


class ArmJogRequest(MotionRequest):
    joint: ArmJoint
    delta: float
    speed: float


class ArmOperationRequest(TypedDict):
    id: str


class ArmActionRequest(MotionRequest):
    targets: Dict[ArmJoint, float]
    duration: float
