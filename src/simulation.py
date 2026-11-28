"""Run camera-free Lite3 simulation on IPv4 loopback."""
import argparse
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8080,
                        help='HTTP port (default: 8080; use 0 for a free port)')
    options = parser.parse_args(argv)
    if __package__:
        from .server import main as run_server
    else:
        from server import main as run_server
    return run_server(['--env', 'dev', '--simulate', '--source', 'none',
                       '--bind', '127.0.0.1', '--port', str(options.port)])


if __name__ == '__main__':
    sys.exit(main())
