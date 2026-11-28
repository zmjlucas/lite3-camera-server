"""Embed provenance in wheels and preserve it through source distributions."""
import importlib.util
from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist


ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT / 'src' / 'lite3_dev_server'


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builds = load_module('_lite3_sdk_build_info', PACKAGE / '_build_info.py')
bundle = load_module('_lite3_server_build_info', PACKAGE.parent / 'build_info.py')


def write_tools_manifest(directory, info):
    files, unavailable = bundle.read_bundle(directory)
    if unavailable:
        raise ValueError('Missing tools build files: ' + ', '.join(unavailable))
    builds.write_manifest(directory / '_build.json', builds.with_files(info, files), files)


class BuildPy(build_py):
    def run(self):
        if getattr(self, 'editable_mode', False):
            return super().run()
        info = builds.sdk_build_info(PACKAGE)
        tools_info = bundle.server_build_info(PACKAGE.parent)
        sdk_output = (Path(self.build_lib) / 'lite3_dev_server').resolve()
        tools_output = (Path(self.build_lib) / 'lite3_dev_server_tools').resolve()
        for output in (sdk_output, tools_output):
            source = PACKAGE.parent
            if output == source or output in source.parents or source in output.parents:
                raise ValueError('The wheel staging directory must be separate from its sources')
            if output.exists():
                shutil.rmtree(str(output))
        super().run()
        files = list(builds.source_files(sdk_output))
        builds.write_manifest(sdk_output / '_build.json', builds.with_files(info, files), files)
        shutil.copyfile(PACKAGE / '_build_info.py', tools_output / '_build_info.py')
        write_tools_manifest(tools_output, tools_info)


class Sdist(sdist):
    def make_release_tree(self, base_dir, files):
        info = builds.sdk_build_info(PACKAGE)
        tools_info = bundle.server_build_info(PACKAGE.parent)
        super().make_release_tree(base_dir, files)
        directory = Path(base_dir) / 'src'
        builds.write_manifest(directory / 'lite3_dev_server' / '_build.json', info)
        write_tools_manifest(directory, tools_info)


setup(cmdclass={'build_py': BuildPy, 'sdist': Sdist})
