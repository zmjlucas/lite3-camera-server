"""Static consumer fixture. Copy outside the checkout to check the installed SDK."""
from typing import Optional, Tuple
from lite3_dev_server import (
    ArmCapabilities, ArmJoint, ArmOperation, ControlStatus, DevClient, DevClientError,
    DogActionName, DogActionRequest, DogActionStatus, DogActionStatusRequest, DogFeedback, DogPhase,
    Environment, ActionResult, ArmJog, DogAction, ManagedClient, ManagedPhase, SequenceResult,
    PreflightError, PreflightResult, TargetTransport, check_preflight, validate_dog_action,
    validate_velocity, validate_arm_jog, BUILD_INFO, BuildInfo, ArmCachedFeedback, ArmCommissioning,
    ArmActionRequest, DevClientTimeout, CameraClient, CameraClientError, CameraFrameUnavailable,
    CameraHealth, CameraHealthStatus, CameraSource, CameraSourceCatalog, CameraStream,
)
from lite3_dev_server_tools.testing import simulated_server


def isolated_simulation() -> ControlStatus:
    with simulated_server(startup_timeout=10.0) as url, DevClient(url) as robot:
        return robot.status()


def status_phase(url: str) -> Optional[DogPhase]:
    with DevClient(url) as robot:
        state = robot.status()
        if state['mode'] == 'disabled':
            return None
        return state['dog']['phase']


def jog_in_simulation(robot: DevClient, joint: ArmJoint) -> Optional[ControlStatus]:
    state = robot.status()
    if state['mode'] != 'hardware':
        return None
    environment: Environment = state['environment']
    if environment != 'dev' or not state['arm']['simulated']:
        return None
    acquired = robot.acquire('typed-client')
    if acquired['status']['arm']['motion_enabled']:
        return robot.arm_jog(joint, delta=0.5)
    return robot.release()


def arm_elbow(robot: DevClient) -> Optional[float]:
    try:
        state = robot.arm_state()
    except DevClientError as error:
        if error.status == 503:
            return None
        raise
    positions = state['position_deg']
    if state['observed'] and positions is not None:
        return positions['e']
    return None


def arm_diagnostics_capabilities(robot: DevClient) -> Optional[ArmCapabilities]:
    state = robot.status()
    if state['mode'] == 'hardware' and state['environment'] == 'dev' and not state['arm']['simulated']:
        return state['arm'].get('capabilities')
    return None


def poll_arm_operation(robot: DevClient, operation_id: str) -> ArmOperation:
    operation = robot.arm_operation(operation_id)
    if operation['phase'] in ('preflight', 'moving') and robot.lease is not None:
        robot.heartbeat()
    return operation


def cancel_arm_operation(robot: DevClient) -> Optional[ArmOperation]:
    return robot.arm_stop()['arm'].get('operation')


def start_dog_action(robot: DevClient, name: DogActionName) -> Optional[DogActionStatus]:
    state = robot.status()
    if state['mode'] != 'hardware':
        return None
    feedback: DogFeedback = state['dog']['feedback']
    if not state['dog']['actions_enabled'] or not feedback['fresh']:
        return None
    return robot.dog_action(name, speed=0.1, duration=1.0)['dog']['action']


def dog_action_request(lease: str, seq: int, name: DogActionName) -> DogActionRequest:
    return {'lease': lease, 'seq': seq, 'name': name, 'speed': 0.1, 'duration': 1.0}


def lookup_dog_action(robot: DevClient, action_id: str) -> DogActionStatus:
    request: DogActionStatusRequest = {'id': action_id}
    return robot.dog_action_status(request['id'])


def default_dog_action(robot: DevClient, name: DogActionName) -> ControlStatus:
    if name == 'forward':
        return robot.forward()
    if name == 'backward':
        return robot.backward()
    if name == 'turn_left':
        return robot.turn_left()
    return robot.turn_right()


def managed_dog_action(url: str, name: DogActionName) -> ActionResult:
    with ManagedClient(url, owner='typed-managed-client', lease_policy='wait') as robot:
        result: ActionResult = robot.run(DogAction(name, speed=0.1, duration=0.5), timeout=15)
        return result


def managed_arm_pair(url: str, joint: ArmJoint) -> Tuple[ManagedPhase, Tuple[ActionResult, ...]]:
    with ManagedClient(url) as robot:
        sequence: SequenceResult = robot.run_sequence(
            [ArmJog(joint, delta=0.5), ArmJog(joint, delta=-0.5)],
            policy='completed_only', timeout=10,
        )
        phase: ManagedPhase = sequence.phase
        results: Tuple[ActionResult, ...] = sequence.results
        return phase, results


def managed_evidence(result: ActionResult) -> Tuple[
        ManagedPhase, Optional[str], Optional[ControlStatus], Optional[ArmOperation]]:
    return result.phase, result.action_id, result.status, result.operation


def guarded_action(url: str, target: TargetTransport) -> PreflightResult:
    with DevClient(url, target=target) as robot:
        decision = robot.preflight('acquire')
        if not decision['ok']:
            return decision
        acquired = robot.acquire('guarded-client')
        decision = robot.preflight('dog/stand', status=acquired['status'])
        if decision['ok']:
            robot.stand()
        return decision


def offline_cli_rules(state: ControlStatus, name: DogActionName) -> PreflightResult:
    validate_dog_action(name, .1, 1., capabilities=state['dog'].get('capabilities'),
                        limits=state['dog']['limits'])
    validate_velocity(vx=.1, limits=state['dog']['limits'])
    validate_arm_jog('e', 1., capabilities=state['arm'].get('capabilities'))
    return check_preflight(state, 'dog/action', 'simulation', owns_lease=True,
                           parameters={'name': name, 'speed': .1, 'duration': 1.})


def error_details(error: DevClientError) -> Optional[str]:
    if isinstance(error, PreflightError):
        return error.result['reasons'][0]['code']
    code: Optional[str] = error.code
    return code


def installed_build() -> BuildInfo:
    commit: Optional[str] = BUILD_INFO.source_commit
    dirty: Optional[bool] = BUILD_INFO.source_dirty
    return BuildInfo(BUILD_INFO.version, commit, dirty, BUILD_INFO.build_id)


def cached_arm_feedback(robot: DevClient) -> Optional[ArmCachedFeedback]:
    arm = robot.status()['arm']
    commissioning: Optional[ArmCommissioning] = arm.get('commissioning')
    if commissioning is None or arm.get('reconnect_required'):
        return None
    return arm.get('cached_feedback')


def wait_simulated_targets(robot: DevClient) -> Optional[ArmOperation]:
    accepted: ControlStatus = robot.arm_action({'b': 10., 'e': -5.}, duration=3.)
    operation = accepted['arm'].get('operation')
    assert operation is not None
    try:
        return robot.wait_arm_operation(operation['operation_id'], timeout=5., renew_lease=True)
    except DevClientTimeout as error:
        operation_id: str = error.operation_id
        assert operation_id == operation['operation_id']
        return error.operation


def arm_action_request(lease: str, seq: int) -> ArmActionRequest:
    return {'lease': lease, 'seq': seq, 'targets': {'b': 5., 'h': -3.}, 'duration': 2.}


def camera_health(camera: CameraClient) -> CameraHealthStatus:
    health: CameraHealth = camera.health()
    return health['status']


def camera_snapshot(camera: CameraClient) -> Optional[bytes]:
    try:
        return camera.snapshot()
    except CameraFrameUnavailable as error:
        health: CameraHealth = error.health
        status: CameraHealthStatus = health['status']
        if status in ('starting', 'stale', 'error'):
            return None
        raise


def first_camera_frame(camera: CameraClient) -> bytes:
    with camera.stream() as frames:
        stream: CameraStream = frames
        for jpeg in stream:
            return jpeg
    return b''


def select_camera(camera: CameraClient, source: CameraSource) -> CameraSourceCatalog:
    try:
        return camera.select_source(source['id'])
    except CameraClientError as error:
        status: Optional[int] = error.status
        if status == 409:
            return camera.sources()
        raise
