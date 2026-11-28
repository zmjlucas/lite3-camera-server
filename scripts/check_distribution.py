"""Build, install and check the SDK and tools outside this checkout; simulate HTTP only.

Install development tools first: python -m pip install '.[dev]'.
"""
import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tarfile
import venv
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def run(arguments, **kwargs):
    subprocess.run([str(argument) for argument in arguments], check=True, **kwargs)


def wheel_provenance(path, package='lite3_dev_server'):
    with zipfile.ZipFile(str(path)) as wheel:
        return json.loads(wheel.read(package + '/_build.json'))


def previous_release(temporary):
    """A 0.1.0 distribution fixture exercises pip's installed-version comparison."""
    source = temporary / 'previous'
    package = source / 'lite3_dev_server'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text("__version__ = '0.1.0'\n", encoding='utf-8')
    (package / 'previous_release.py').write_text('# Removed by the upgrade.\n', encoding='utf-8')
    (source / 'pyproject.toml').write_text(
        '[build-system]\nrequires = ["setuptools>=61"]\nbuild-backend = "setuptools.build_meta"\n'
        '[project]\nname = "lite3-dev-server"\nversion = "0.1.0"\n', encoding='utf-8')
    wheels = source / 'dist'
    run([sys.executable, '-m', 'build', '--wheel', '--no-isolation', '--outdir', wheels, source])
    wheel, = wheels.glob('*.whl')
    return wheel


def main():
    run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests',
         '-p', 'test_distribution_builds.py', '-v'], cwd=ROOT)
    # All source checks use the declared Python 3.8 consumer contract.
    run([sys.executable, '-m', 'mypy', '--strict', 'src/lite3_dev_server'], cwd=ROOT)
    run([sys.executable, '-m', 'mypy', '--strict', 'src/testing.py'], cwd=ROOT)
    run([sys.executable, '-m', 'mypy', '--follow-imports=silent',
         'src/application.py', 'src/control.py', 'src/arm_gateway.py', 'src/arm_actions.py'], cwd=ROOT)

    with tempfile.TemporaryDirectory(prefix='lite3-sdk-check-') as directory:
        temporary = Path(directory)
        wheels = temporary / 'wheels'
        run([sys.executable, '-m', 'build', '--wheel', '--no-isolation', '--outdir', wheels, ROOT], cwd=temporary)
        wheel, = wheels.glob('*.whl')
        provenance = wheel_provenance(wheel)
        assert provenance['version'] == (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
        # The sdist is rebuilt outside Git, with only the files shipped in it.
        archives = temporary / 'sdists'
        run([sys.executable, '-m', 'build', '--sdist', '--no-isolation', '--outdir', archives, ROOT], cwd=temporary)
        archive, = archives.glob('*.tar.gz')
        unpacked = temporary / 'unpacked'
        with tarfile.open(str(archive)) as source_archive:
            source_archive.extractall(str(unpacked))
        source, = unpacked.iterdir()
        rebuilt = temporary / 'rebuilt'
        run([sys.executable, '-m', 'build', '--wheel', '--no-isolation', '--outdir', rebuilt, source], cwd=temporary)
        rebuilt_wheel, = rebuilt.glob('*.whl')
        assert wheel_provenance(rebuilt_wheel) == provenance, 'Source distribution lost SDK provenance'
        tools_package = 'lite3_dev_server_tools'
        assert wheel_provenance(rebuilt_wheel, tools_package) == wheel_provenance(wheel, tools_package), (
            'Source distribution lost tools provenance')
        environment = temporary / 'environment'
        # Managed macOS interpreters resolve libpython relative to their real executable.
        venv.EnvBuilder(with_pip=True, symlinks=os.name != 'nt').create(str(environment))
        python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        run([python, '-I', '-m', 'pip', 'install', '--no-index', previous_release(temporary)], cwd=temporary)
        run([python, '-I', '-c', 'from importlib.metadata import version; '
             'assert version("lite3-dev-server") == "0.1.0"'], cwd=temporary)
        # Exercise the upgrade by name and require the standard-library tools to install offline.
        run([python, '-I', '-m', 'pip', 'install', '--upgrade', '--no-index',
             '--find-links', rebuilt, 'lite3-dev-server[tools]'], cwd=temporary)

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

        expected = consumer / 'expected-build.json'
        expected.write_text(json.dumps(provenance), encoding='utf-8')
        run([python, '-I', '-B', consumer / 'installed_tools_smoke.py'],
            cwd=consumer, env=environment_variables)
    print('PASS: SDK types, wheel and sdist provenance, pip upgrade, installed tools and deployed simulation')


if __name__ == '__main__':
    main()
