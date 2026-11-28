"""Build, install and check the SDK and tools outside this checkout; simulate HTTP only.

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
    run([sys.executable, '-m', 'mypy', '--strict', 'src/testing.py'], cwd=ROOT)
    run([sys.executable, '-m', 'mypy', '--follow-imports=silent',
         'src/application.py', 'src/control.py', 'src/arm_gateway.py'], cwd=ROOT)

    with tempfile.TemporaryDirectory(prefix='lite3-sdk-check-') as directory:
        temporary = Path(directory)
        wheels = temporary / 'wheels'
        # Build the wheel from the sdist so both release formats must contain the tools.
        run([sys.executable, '-m', 'build', '--no-isolation', '--outdir', wheels, ROOT], cwd=temporary)
        wheel, = wheels.glob('*.whl')
        environment = temporary / 'environment'
        # Managed macOS interpreters resolve libpython relative to their real executable.
        venv.EnvBuilder(with_pip=True, symlinks=os.name != 'nt').create(str(environment))
        python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        # With no index and no preinstalled runtime packages, mandatory tool dependencies fail here.
        run([python, '-I', '-m', 'pip', 'install', '--no-index', str(wheel) + '[tools]'], cwd=temporary)

        consumer = temporary / 'consumer'
        consumer.mkdir()
        for name in ('sdk_typecheck.py', 'installed_sdk_smoke.py', 'installed_tools_smoke.py'):
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

        run([python, '-I', '-B', consumer / 'installed_tools_smoke.py'],
            cwd=consumer, env=environment_variables)
    print('PASS: SDK types, sdist, wheel, installed tools and deployed simulation')


if __name__ == '__main__':
    main()
