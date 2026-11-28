"""Standard-library build identity, also copied into Python 3.6 device bundles."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Iterable, NamedTuple, Optional, Tuple


class BuildInfo(NamedTuple):
    version: str
    source_commit: Optional[str]
    source_dirty: Optional[bool]
    build_id: str


UNKNOWN_BUILD = BuildInfo('unknown', None, None, 'unknown')
_VERSION = r'[0-9]+\.[0-9]+\.[0-9]+'


def read_manifest(path: Path, files: Optional[Iterable[Tuple[str, bytes]]] = None) -> Optional[BuildInfo]:
    """Ignore absent or malformed provenance; never trust it as header text."""
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or set(data) != set(BuildInfo._fields) | {'files'}:
            return None
        version, commit, dirty, build_id = (data[field] for field in BuildInfo._fields)
        if not isinstance(version, str) or not re.fullmatch(_VERSION + r'|unknown', version):
            return None
        if commit is not None and (not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit)):
            return None
        if (dirty is not None and type(dirty) is not bool) or ((commit is None) != (dirty is None)):
            return None
        if not isinstance(build_id, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', build_id):
            return None
        info = BuildInfo(version, commit, dirty, build_id)
        contents = list(source_files(path.parent) if files is None else files)
        if data['files'] != sorted(name for name, _ in contents) or with_files(info, contents).build_id != build_id:
            return None
        return info
    except (OSError, ValueError, RecursionError):
        return None


def write_manifest(path: Path, info: BuildInfo,
                   files: Optional[Iterable[Tuple[str, bytes]]] = None) -> None:
    data = info._asdict()
    data['files'] = sorted(name for name, _ in (source_files(path.parent) if files is None else files))
    path.write_text(json.dumps(data, sort_keys=True) + '\n', encoding='utf-8')


def _source_revision(root: Path) -> Tuple[Optional[str], Optional[bool]]:
    # Git otherwise walks into an unrelated parent checkout after a source copy.
    if not (root / '.git').exists():
        return None, None
    try:
        def git(*arguments: str) -> str:
            return subprocess.run(['git', '-C', str(root), *arguments], check=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  universal_newlines=True, timeout=3).stdout.strip()
        if Path(git('rev-parse', '--show-toplevel')).resolve() != root.resolve():
            return None, None
        commit = git('rev-parse', '--verify', 'HEAD')
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit):
            return None, None
        return commit, bool(git('status', '--porcelain', '--untracked-files=normal'))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, None


def _release_version(root: Path) -> str:
    try:
        version = (root / 'VERSION').read_text(encoding='utf-8').strip()
        if not re.fullmatch(_VERSION, version):
            version = 'unknown'
    except (OSError, ValueError):
        version = 'unknown'
    return version


def with_files(info: BuildInfo, files: Iterable[Tuple[str, bytes]]) -> BuildInfo:
    """Identify a captured payload using its already established provenance."""
    digest = hashlib.sha256()
    digest.update(json.dumps([info.version, info.source_commit, info.source_dirty],
                            separators=(',', ':')).encode('utf-8'))
    for name, content in sorted(files):
        # Length-prefix each input so names or content cannot alias file boundaries.
        for value in (name.encode('utf-8'), content):
            digest.update(str(len(value)).encode('ascii') + b':' + value)
    return info._replace(build_id='sha256:' + digest.hexdigest())


def capture_build(root: Path, files: Iterable[Tuple[str, bytes]]) -> BuildInfo:
    """Fingerprint the supplied byte snapshot together with its source revision."""
    commit, dirty = _source_revision(root)
    return with_files(BuildInfo(_release_version(root), commit, dirty, 'unknown'), files)


def source_build_info(root: Path, files: Iterable[Tuple[str, bytes]],
                      manifest: Optional[BuildInfo]) -> BuildInfo:
    """Preserve a validated archive identity when its release still agrees."""
    if manifest is not None and manifest.version == _release_version(root):
        return manifest
    return capture_build(root, files)


def source_files(directory: Path) -> Iterable[Tuple[str, bytes]]:
    for path in sorted(directory.rglob('*')):
        if path.is_file() and (path.suffix in ('.py', '.html', '.js') or path.name == 'py.typed'):
            yield path.relative_to(directory).as_posix(), path.read_bytes()


def sdk_build_info(package: Path) -> BuildInfo:
    manifest = read_manifest(package / '_build.json')
    root = package.parent.parent
    if package == root / 'src' / 'lite3_dev_server' and (root / 'VERSION').is_file():
        return source_build_info(root, source_files(package), manifest)
    return manifest if manifest is not None else UNKNOWN_BUILD
