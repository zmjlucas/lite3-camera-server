"""Typed Python client and JSON contracts for Lite3 Dev Server."""
from pathlib import Path as _Path

from ._build_info import BuildInfo, sdk_build_info as _sdk_build_info
from .api import (
    AcquireRequest, AcquireResponse, ArmActionRequest, ArmCachedFeedback, ArmCapabilities,
    ArmCommissioning, ArmDiagnosticOperation, ArmFeedbackJoint, ArmGatewayReconnect,
    ArmJogRequest, ArmJoint, ArmObservationStatus, ArmOperation, ArmOperationRequest,
    ArmReconnectResponse, ArmSample, ArmState, ArmStatus, CAMERA_PROTOCOL_HEADER,
    CAMERA_PROTOCOL_VERSION, CameraBackend, CameraHealth, CameraHealthStatus,
    CameraSource, CameraSourceCatalog, ControlMode, ControlStatus, DogActionCapability,
    DogActionName, DogActionRequest, DogActionStatus, DogActionStatusRequest, DogCapabilities,
    DogFeedback, DogPhase, DogStatus, Environment, ErrorResponse, LeaseRequest, MotionOperation,
    MotionRequest, ObservedArmState, PreflightReason, PreflightResult, PROTOCOL_HEADER,
    PROTOCOL_VERSION, ResetRequest, SimulatedArmOperation, StopRequest, TargetTransport,
    UnobservedArmState, VelocityAxis, VelocityLimit, VelocityRequest,
)
from .camera import CameraClient, CameraClientError, CameraFrameUnavailable, CameraStream
from .client import DevClient, DevClientError, DevClientTimeout, PreflightError
from .managed import (
    ActionResult, ArmJog, DogAction, LeasePolicy, ManagedAction, ManagedClient, ManagedPhase,
    SequencePolicy, SequenceResult,
)
from .preflight import check_preflight
from .validation import validate_arm_jog, validate_dog_action, validate_velocity

BUILD_INFO = _sdk_build_info(_Path(__file__).resolve().parent)
__version__ = BUILD_INFO.version

__all__ = [
    'BUILD_INFO', 'BuildInfo', '__version__',
    'AcquireRequest', 'AcquireResponse', 'ActionResult', 'ArmActionRequest', 'ArmCachedFeedback',
    'ArmCapabilities', 'ArmCommissioning', 'ArmDiagnosticOperation', 'ArmFeedbackJoint',
    'ArmGatewayReconnect', 'ArmJog', 'ArmJogRequest', 'ArmJoint', 'ArmObservationStatus',
    'ArmOperation', 'ArmOperationRequest', 'ArmReconnectResponse', 'ArmSample', 'ArmState',
    'ArmStatus', 'CAMERA_PROTOCOL_HEADER', 'CAMERA_PROTOCOL_VERSION', 'CameraBackend',
    'CameraClient', 'CameraClientError', 'CameraFrameUnavailable', 'CameraHealth',
    'CameraHealthStatus', 'CameraSource', 'CameraSourceCatalog', 'CameraStream', 'ControlMode',
    'ControlStatus', 'DevClient', 'DevClientError', 'DevClientTimeout', 'DogAction',
    'DogActionCapability', 'DogActionName', 'DogActionRequest', 'DogActionStatus',
    'DogActionStatusRequest', 'DogCapabilities', 'DogFeedback', 'DogPhase', 'DogStatus',
    'Environment', 'ErrorResponse', 'LeasePolicy', 'LeaseRequest', 'ManagedAction',
    'ManagedClient', 'ManagedPhase', 'MotionOperation', 'MotionRequest', 'ObservedArmState',
    'PreflightError', 'PreflightReason', 'PreflightResult', 'PROTOCOL_HEADER',
    'PROTOCOL_VERSION', 'ResetRequest', 'SequencePolicy', 'SequenceResult', 'SimulatedArmOperation',
    'StopRequest', 'TargetTransport', 'UnobservedArmState', 'VelocityAxis', 'VelocityLimit',
    'VelocityRequest', 'check_preflight', 'validate_arm_jog', 'validate_dog_action',
    'validate_velocity',
]
