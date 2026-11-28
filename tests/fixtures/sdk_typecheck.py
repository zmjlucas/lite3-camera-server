"""Static consumer fixture. Copy outside the checkout to check the installed SDK."""
from typing import Optional

from lite3_dev_server import (
    ArmCapabilities, ArmJoint, ArmOperation, ControlStatus, DevClient, DevClientError,
    DogActionName, DogActionRequest, DogActionStatus, DogActionStatusRequest, DogFeedback, DogPhase, Environment,
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
