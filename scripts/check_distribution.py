"""Build, install and type-check the SDK outside this checkout; simulate HTTP only.

Install development tools first: python -m pip install '.[dev]'.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import venv


ROOT = Path(__file__).resolve().parents[1]


def run(arguments, **kwargs):
    subprocess.run([str(argument) for argument in arguments], check=True, **kwargs)


def main():
    # All source checks use the declared Python 3.8 consumer contract.
    run([sys.executable, '-m', 'mypy', '--strict', 'src/lite3_dev_server'], cwd=ROOT)
    run([sys.executable, '-m', 'mypy', '--follow-imports=silent',
         'src/application.py', 'src/control.py', 'src/arm_gateway.py'], cwd=ROOT)

    with tempfile.TemporaryDirectory(prefix='lite3-sdk-check-') as directory:
        temporary = Path(directory)
        wheels = temporary / 'wheels'
        run([sys.executable, '-m', 'build', '--wheel', '--no-isolation', '--outdir', wheels, ROOT], cwd=temporary)
        wheel, = wheels.glob('*.whl')
        environment = temporary / 'environment'
        # Managed macOS interpreters resolve libpython relative to their real executable.
        venv.EnvBuilder(with_pip=True, symlinks=os.name != 'nt').create(str(environment))
        python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        run([python, '-I', '-m', 'pip', 'install', '--no-index', '--no-deps', wheel], cwd=temporary)

        consumer = temporary / 'consumer'
        consumer.mkdir()
        for name in ('sdk_typecheck.py', 'installed_sdk_smoke.py'):
            shutil.copy2(str(ROOT / 'tests/fixtures' / name), str(consumer / name))
        # Do not inherit source lookup paths when checking the installed SDK.
        environment_variables = dict(os.environ)
        environment_variables.pop('MYPYPATH', None)
        environment_variables.pop('PYTHONPATH', None)
        config = consumer / 'mypy.ini'
        config.write_text('[mypy]\npython_version = 3.8\nstrict = True\n', encoding='utf-8')
        run([sys.executable, '-m', 'mypy', '--config-file', config,
             '--python-executable', python, 'sdk_typecheck.py'],
            cwd=consumer, env=environment_variables)

        # Use the same allowlisted artifacts as robot deployment, without SSH.
        sys.path.insert(0, str(ROOT))
        from src.launcher import SERVER, SERVER_BUNDLE
        deployed = temporary / 'deployed'
        deployed.mkdir()
        for source, _ in SERVER_BUNDLE:
            destination = deployed / source.relative_to(SERVER.parent)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(source), str(destination))
        run([python, '-I', '-B', consumer / 'installed_sdk_smoke.py', deployed / 'server.py'],
            cwd=consumer, env=environment_variables)
    print('PASS: SDK source types, wheel, isolated consumer types and deployed simulation')


if __name__ == '__main__':
    main()
