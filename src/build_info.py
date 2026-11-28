"""Content identity and release provenance for the native server bundle."""
from functools import lru_cache
import hashlib
import importlib.util
from pathlib import Path
import stat

# Upload dependencies first and the entry point last. The SDK and test fixtures
# have separate distribution lifecycles and are excluded from this identity.
BUNDLE_FILES = (
    "_build_info.py", "build_info.py", "runtime_info.py", "wide_angle.py",
    "arm_actions.py", "dog_actions.py", "dog_state.py", "camera_sources.py",
    "camera.py", "application.py", "http_server.py", "arm_motion.py",
    "web/camera.html", "control.py", "arm_gateway.py", "remote/arm_worker.py",
    "web/control.html", "web/dev_client.js", "server.py",
)


def bundle_path(directory, name):
    path = directory / name
    if name == "_build_info.py" and directory.name == "src" and not path.exists():
        return directory / "lite3_dev_server" / name
    return path


def build_id(contents):
    """Hash captured (relative POSIX path, bytes) pairs independent of location."""
    digest = hashlib.sha256()
    for name, content in sorted(contents):
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(str(len(content)).encode("ascii") + b"\0")
        digest.update(content)
    return "sha256:" + digest.hexdigest()


def read_bundle(directory):
    """Read regular bundle files and report unavailable paths."""
    files = []
    unavailable = []
    for name in BUNDLE_FILES:
        try:
            path = bundle_path(directory, name)
            if stat.S_ISREG(path.lstat().st_mode):
                files.append((name, path.read_bytes()))
                continue
        except OSError:
            pass
        unavailable.append(name)
    return files, unavailable


@lru_cache(maxsize=1)
def _provenance():
    path = bundle_path(Path(__file__).resolve().parent, "_build_info.py")
    spec = importlib.util.spec_from_file_location("_lite3_build_info", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recorded_build(directory, files):
    provenance = _provenance()
    manifest = provenance.read_manifest(directory / "_build.json", files)
    if directory.name == "src" and (directory.parent / "VERSION").is_file():
        return provenance.source_build_info(directory.parent, files, manifest)
    return manifest


def server_build_info(directory):
    """Resolve a build manifest or identify a complete source checkout."""
    files, unavailable = read_bundle(directory)
    build = None if unavailable else _recorded_build(directory, files)
    return build if build is not None else _provenance().UNKNOWN_BUILD


def write_deployment_manifest(directory, path, files):
    """Identify the exact upload, preserving its packaged source revision."""
    provenance = _provenance()
    build = _recorded_build(directory, files)
    if build is None:
        build = provenance.capture_build(directory.parent, files)
    provenance.write_manifest(path, build, files)
    return build
