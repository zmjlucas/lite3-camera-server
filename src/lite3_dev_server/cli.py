"""Installed control commands. All network and ownership operations use DevClient."""
import argparse
from contextlib import contextmanager
import json
import math
import signal
import sys
import time
from types import FrameType
from typing import Callable, Dict, Iterator, List, Literal, NoReturn, Optional, Sequence, TypeVar, cast
from urllib.parse import urlsplit

from .api import ArmJoint, ControlStatus, DogActionName
from .client import DevClient, DevClientError
from .validation import validate_arm_jog, validate_dog_action


_DOG_ACTIONS = ('forward', 'backward', 'turn_left', 'turn_right')
_Result = TypeVar('_Result')


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


def _common(parser: argparse.ArgumentParser, root: bool = False) -> None:
    parser.add_argument('--url', metavar='ORIGIN',
                        default='http://127.0.0.1:8080' if root else argparse.SUPPRESS,
                        help='HTTP(S) service origin (default: http://127.0.0.1:8080)')
    parser.add_argument('--transport', choices=('simulated', 'real'),
                        default=None if root else argparse.SUPPRESS, help='required transport for motion')
    parser.add_argument('--timeout', type=float, metavar='SECONDS', default=2. if root else argparse.SUPPRESS,
                        help='per-request timeout, at most 10 s (default: 2)')
    parser.add_argument('--wait-timeout', type=float, metavar='SECONDS', default=15. if root else argparse.SUPPRESS,
                        help='per-stage wait limit, at most 60 s (default: 15)')
    parser.add_argument('--execute', action='store_true', default=False if root else argparse.SUPPRESS,
                        help='explicitly authorize motion or reset')
    parser.add_argument('--dry-run', action='store_true', default=False if root else argparse.SUPPRESS,
                        help='validate and print requests offline')


def _parser() -> argparse.ArgumentParser:
    # Disable abbreviation on every level: --exec must never authorize motion.
    parser = _Parser(prog='lite3-control', allow_abbrev=False, description=(
        'Query service status by default. Execute one bounded dog action or R5 operation. '
        'Results are JSON; motion requires --execute and --transport.'))
    _common(parser, root=True)
    parser.set_defaults(command='status')
    commands = parser.add_subparsers()
    status = commands.add_parser('status', allow_abbrev=False, help='read service status without serial feedback')
    _common(status)
    for group, help_text in (('dog', 'timed dog actions and stop'), ('arm', 'R5 feedback and control')):
        parent = commands.add_parser(group, allow_abbrev=False, help=help_text)
        _common(parent)
        children = parent.add_subparsers(required=True)
        names = _DOG_ACTIONS + ('stop',) if group == 'dog' else ('state', 'reconnect', 'operation', 'jog', 'stop')
        for name in names:
            child = children.add_parser(name, allow_abbrev=False)
            _common(child)
            child.set_defaults(command=group + '.' + name)
            if group == 'dog' and name in _DOG_ACTIONS:
                child.add_argument('--speed', type=float, required=True, help='positive m/s or rad/s for turns')
                child.add_argument('--duration', type=float, required=True, help='seconds, at most 10')
            if group == 'arm' and name == 'jog':
                child.add_argument('joint', choices=('b', 's', 'e', 'w', 'h'))
                child.add_argument('--delta', type=float, required=True, help='relative degrees')
                child.add_argument('--speed', type=float, required=True, help='degrees per second')
            if group == 'arm' and name == 'operation':
                child.add_argument('operation_id')
    for name in ('estop', 'reset'):
        child = commands.add_parser(name, allow_abbrev=False)
        _common(child)
        child.set_defaults(command=name)
        if name == 'reset':
            child.add_argument('--revision', required=True, help='safety_revision explicitly observed in status')
    return parser


def _is_motion(args: argparse.Namespace) -> bool:
    return args.command in tuple('dog.' + name for name in _DOG_ACTIONS) + ('arm.jog',)


def _positive(value: float, maximum: float, name: str) -> None:
    if not math.isfinite(value) or not 0 < value <= maximum:
        raise ValueError('{} must be finite, greater than zero and at most {}'.format(name, maximum))


def _validate(args: argparse.Namespace) -> DevClient:
    # Validate the origin before constructing requests, including on an offline run.
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in args.url):
        raise ValueError('--url must contain no whitespace or control characters')
    if any(character in args.url for character in ('?', '#', '\\')):
        raise ValueError('--url must be a server origin without query, fragment or backslash')
    port = urlsplit(args.url).port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('--url port must be between 1 and 65535')
    client = DevClient(args.url, timeout=args.timeout)
    _positive(args.wait_timeout, 60., '--wait-timeout')
    if _is_motion(args) or args.command == 'reset':
        if args.transport is None:
            raise ValueError('Motion and reset require explicit --transport simulated or real')
        if not args.execute and not args.dry_run:
            raise ValueError('Motion and reset require the full --execute flag (or use --dry-run)')
    if args.command in tuple('dog.' + name for name in _DOG_ACTIONS):
        validate_dog_action(cast(DogActionName, args.command.split('.')[1]), args.speed, args.duration)
    if args.command == 'arm.jog':
        validate_arm_jog(cast(ArmJoint, args.joint), args.delta, args.speed)
        if args.delta == 0:
            raise ValueError('absolute --delta must be greater than zero')
        if args.transport == 'real':
            if args.joint not in ('b', 'e', 'h'):
                raise ValueError('Real R5 diagnostics support only b, e and h')
            maximum = 2. if args.joint == 'e' else .5
            if not .1 <= abs(args.delta) <= maximum:
                raise ValueError('Real R5 absolute --delta must be between 0.1 and {}'.format(maximum))
            _positive(args.speed, 1., 'real R5 --speed')
            if 3. + abs(args.delta) / args.speed > 8.:
                raise ValueError('Real R5 operation budget (3 + abs(delta) / speed) must be at most 8 s')
    if args.command == 'arm.operation' and not 1 <= len(args.operation_id) <= 128:
        raise ValueError('operation ID must contain 1 to 128 characters')
    if args.command == 'reset' and (not 1 <= len(args.revision) <= 128 or not args.revision.isascii()):
        raise ValueError('--revision must be an observed ASCII safety revision of 1 to 128 characters')
    return client


def _preview(args: argparse.Namespace, client: DevClient) -> Dict[str, object]:
    requests: List[Dict[str, object]] = [{'method': 'GET', 'path': '/api/status'}]

    def post(path: str, body: Dict[str, object], **metadata: object) -> None:
        requests.append(dict(method='POST', path='/api/' + path, body=body, **metadata))

    if _is_motion(args):
        post('control/acquire', {'owner': 'lite3-control'})
        if args.command.startswith('dog.'):
            post('dog/stand', {'lease': '<SDK lease>', 'seq': '<SDK sequence>'})
            post('control/heartbeat', {'lease': '<SDK lease>'}, repeat='until standing readiness')
            post('dog/action', {'lease': '<SDK lease>', 'seq': '<SDK sequence>',
                                'name': args.command.split('.')[1], 'speed': args.speed, 'duration': args.duration})
            post('control/heartbeat', {'lease': '<SDK lease>'}, repeat='while action is running')
        else:
            post('arm/jog', {'lease': '<SDK lease>', 'seq': '<SDK sequence>',
                             'joint': args.joint, 'delta': args.delta, 'speed': args.speed})
            if args.transport == 'real':
                post('control/heartbeat', {'lease': '<SDK lease>'}, repeat='while operation runs and lease is held')
                post('arm/operation', {'id': '<acknowledged operation ID>'}, repeat='until terminal phase')
        post('control/release', {'lease': '<SDK lease>'}, when='SDK still holds this lease on exit')
    elif args.command != 'status':
        path = args.command.replace('.', '/') if '.' in args.command else 'control/' + args.command
        body: Dict[str, object] = {}
        if args.command == 'arm.operation':
            body['id'] = args.operation_id
        if args.command == 'reset':
            body['revision'] = args.revision
        post(path, body)
    return dict(dry_run=True, url=client.url, transport=args.transport, requests=requests,
                timeout=args.timeout, wait_timeout=args.wait_timeout)


def _error(command: Optional[str], message: str, kind: str) -> Dict[str, object]:
    print('lite3-control: ' + message, file=sys.stderr)
    return dict(ok=False, command=command, kind=kind, error=message)


class _Interrupted(Exception):
    pass


class _Cancellation:
    def __init__(self) -> None:
        self.number: Optional[int] = None

    def handle(self, number: int, frame: Optional[FrameType]) -> None:
        # Finish a pending SDK call so acquisition can record its lease. Cleanup
        # runs outside the handler and further signals cannot interrupt release.
        if self.number is None:
            self.number = number

    def check(self) -> None:
        if self.number is not None:
            raise _Interrupted()

    def call(self, function: Callable[..., _Result], *args: object) -> _Result:
        self.check()
        result = function(*args)
        self.check()
        return result


@contextmanager
def _handle_signals() -> Iterator[_Cancellation]:
    cancellation = _Cancellation()
    numbers = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, 'SIGHUP'):
        numbers.append(signal.SIGHUP)
    previous = {number: signal.getsignal(number) for number in numbers}
    try:
        for number in numbers:
            signal.signal(number, cancellation.handle)
        yield cancellation
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


class _ExecutionError(DevClientError):
    def __init__(self, message: str, result: object, status: Optional[int] = None) -> None:
        super().__init__(message, status)
        self.result = result


def _check_transport(args: argparse.Namespace, state: ControlStatus) -> None:
    if args.transport is None:
        return
    parts = ('dog',) if args.command.startswith('dog.') else (
        ('arm',) if args.command.startswith('arm.') else ('dog', 'arm'))
    for part in parts:
        simulated = state['dog']['simulated'] if part == 'dog' else state['arm']['simulated']
        if simulated != (args.transport == 'simulated'):
            raise DevClientError('{} transport mismatch: requested {}, service reports {}'.format(
                part, args.transport, 'simulated' if simulated else 'real'))


def _check_motion(args: argparse.Namespace, state: ControlStatus) -> None:
    _check_transport(args, state)
    if state['mode'] != 'hardware' or state['estop']:
        raise DevClientError('Motion requires hardware control mode with the emergency stop cleared')
    if args.command.startswith('dog.'):
        if not state['dog']['motion_enabled']:
            raise DevClientError('Dog motion is unavailable')
    else:
        arm = state['arm']
        if state['environment'] != 'dev' or not arm['motion_enabled']:
            raise DevClientError('Arm motion is unavailable: ' + (arm['reason'] or 'dev mode is required'))
        if not arm['simulated']:
            capabilities = arm.get('capabilities')
            joint = cast(Literal['b', 'e', 'h'], args.joint)
            if (capabilities is None or joint not in capabilities['joints'] or
                    abs(args.delta) > capabilities['max_delta_deg'][joint] or
                    args.speed > capabilities['max_speed_deg_s'] or state['dog']['motion_enabled']):
                raise DevClientError('Arm motion requires compatible commissioned diagnostic capabilities')


def _check_deadline(cancellation: _Cancellation, deadline: float, stage: str) -> None:
    cancellation.check()
    if time.monotonic() >= deadline:
        raise DevClientError(stage + ' timed out')


def _pause(cancellation: _Cancellation, deadline: float, stage: str) -> None:
    _check_deadline(cancellation, deadline, stage)
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(min(.1, remaining))
    _check_deadline(cancellation, deadline, stage)


def _dog_action(args: argparse.Namespace, client: DevClient, cancellation: _Cancellation) -> ControlStatus:
    state = cancellation.call(client.stand)
    deadline = time.monotonic() + args.wait_timeout
    while not state['dog']['actions_enabled']:
        if state['dog']['phase'] == 'error' or not state['lease_active'] or state['estop']:
            raise _ExecutionError('Dog preparation failed', state)
        _pause(cancellation, deadline, 'Dog preparation')
        state = cancellation.call(client.heartbeat)
        _check_deadline(cancellation, deadline, 'Dog preparation')
    _check_deadline(cancellation, deadline, 'Dog preparation')
    _check_motion(args, state)
    state = cancellation.call(client.dog_action, cast(DogActionName, args.command.split('.')[1]),
                              args.speed, args.duration)
    action = state['dog']['action']
    if action is None:
        raise DevClientError('Service did not acknowledge a dog action')
    action_id = action['id']
    deadline = time.monotonic() + args.wait_timeout
    while action['phase'] == 'running':
        _pause(cancellation, deadline, 'Dog action')
        try:
            state = cancellation.call(client.heartbeat)
        except DevClientError as error:
            cancellation.check()
            if error.status != 409:
                raise
            _check_deadline(cancellation, deadline, 'Dog action')
            # Completion revokes ownership and can race the next heartbeat.
            state = cancellation.call(client.status)
            ended = state['dog']['action']
            if ended is None or ended['id'] != action_id or ended['phase'] == 'running':
                raise error
        _check_deadline(cancellation, deadline, 'Dog action')
        action = state['dog']['action']
        if action is None or action['id'] != action_id:
            raise _ExecutionError('Dog action was superseded', state)
    if action['phase'] != 'completed':
        raise _ExecutionError('Dog action {}: {}'.format(action['phase'], action['reason']), state)
    return state


def _arm_jog(args: argparse.Namespace, client: DevClient, cancellation: _Cancellation) -> object:
    state = cancellation.call(client.arm_jog, cast(ArmJoint, args.joint), args.delta, args.speed)
    if state['arm']['simulated']:
        return state
    operation = state['arm'].get('operation')
    if operation is None:
        raise DevClientError('Service did not acknowledge an arm operation')
    operation_id = operation['operation_id']
    deadline = time.monotonic() + args.wait_timeout
    revoked: Optional[DevClientError] = None
    try:
        while operation['phase'] in ('preflight', 'moving', 'stopping'):
            _pause(cancellation, deadline, 'Arm operation')
            if revoked is None:
                try:
                    cancellation.call(client.heartbeat)
                except DevClientError as error:
                    cancellation.check()
                    if error.status != 409:
                        raise
                    # Observer stops and expiry revoke the lease. The acknowledged
                    # operation can still report cancellation/hold evidence.
                    revoked = error
                _check_deadline(cancellation, deadline, 'Arm operation')
            try:
                observed = cancellation.call(client.arm_operation, operation_id)
            except DevClientError:
                if revoked is not None:
                    raise revoked
                raise
            if observed['operation_id'] != operation_id:
                if revoked is not None:
                    raise revoked
                raise DevClientError('Arm operation was superseded')
            operation = observed
            _check_deadline(cancellation, deadline, 'Arm operation')
    except DevClientError as error:
        # Preserve the last acknowledged operation and its ID for later inspection.
        raise _ExecutionError(str(error), operation, error.status) from None
    if operation['phase'] not in ('completed', 'commanded_unverified'):
        raise _ExecutionError('Arm operation {}: {}'.format(operation['phase'], operation['reason']), operation)
    return operation


def _execute(args: argparse.Namespace, client: DevClient, cancellation: _Cancellation) -> object:
    state = cancellation.call(client.status)
    _check_transport(args, state)
    if args.command == 'status':
        return state
    if _is_motion(args):
        _check_motion(args, state)
        acquired = cancellation.call(client.acquire, 'lite3-control')
        _check_motion(args, acquired['status'])
        if args.command.startswith('dog.'):
            return _dog_action(args, client, cancellation)
        return _arm_jog(args, client, cancellation)
    if args.command == 'arm.operation':
        return cancellation.call(client.arm_operation, args.operation_id)
    if args.command == 'reset':
        return cancellation.call(client.reset, args.revision)
    commands: Dict[str, Callable[[], object]] = {
        'dog.stop': client.stop, 'arm.stop': client.arm_stop, 'estop': client.estop,
        'arm.state': client.arm_state, 'arm.reconnect': client.arm_reconnect,
    }
    return cancellation.call(commands[args.command])


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Print one JSON result and return 0, 1, 2, or 128 + interrupting signal."""
    args = None
    try:
        args = _parser().parse_args(argv)
        client = _validate(args)
    except SystemExit as help_exit:
        return int(help_exit.code or 0)
    except ValueError as error:
        print(json.dumps(_error(args.command if args else None, str(error), 'argument_error')))
        return 2
    if args.dry_run:
        print(json.dumps(dict(ok=True, command=args.command, result=_preview(args, client)), allow_nan=False))
        return 0
    output: Dict[str, object] = {}
    code = 0
    with _handle_signals() as cancellation:
        try:
            output = dict(ok=True, command=args.command, result=_execute(args, client, cancellation))
        except _Interrupted:
            pass
        except DevClientError as error:
            output = _error(args.command, str(error), 'execution_error')
            if error.status is not None:
                output['http_status'] = error.status
            if isinstance(error, _ExecutionError):
                output['result'] = error.result
            code = 1
        finally:
            try:
                client.close()
            except DevClientError as cleanup_error:
                if code == 0 and cancellation.number is None:
                    output = _error(args.command, str(cleanup_error), 'execution_error')
                    code = 1
                else:
                    print('lite3-control: lease cleanup failed: ' + str(cleanup_error), file=sys.stderr)
                output['cleanup_error'] = str(cleanup_error)
        if cancellation.number is not None:
            interrupted = _error(args.command, 'Interrupted by ' + signal.Signals(cancellation.number).name,
                                 'interrupted')
            interrupted['signal'] = cancellation.number
            if 'cleanup_error' in output:
                interrupted['cleanup_error'] = output['cleanup_error']
            output = interrupted
            code = 128 + cancellation.number
    print(json.dumps(output, allow_nan=False))
    return code
