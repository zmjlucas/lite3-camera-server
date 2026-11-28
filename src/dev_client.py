"""Compatibility imports for checkout users of the Lite3 Dev Server SDK."""
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
import sys
from types import ModuleType

_source = Path(__file__).resolve().parent
_spec = find_spec('lite3_dev_server')
if _spec and _spec.origin and Path(_spec.origin).resolve().parent.parent == _source:
    from lite3_dev_server import DevClient, DevClientError
elif __package__ and any(Path(path).resolve() == _source
                         for path in getattr(sys.modules.get(__package__), '__path__', ())):
    from .lite3_dev_server import DevClient, DevClientError
else:
    # A pinned file import may lack a search path for its sibling SDK.
    # Use a checkout-specific namespace without changing sys.path.
    _namespace = '_lite3_checkout_{:x}'.format(abs(hash(_source)))
    _package = ModuleType(_namespace)
    _package.__path__ = [str(_source)]
    sys.modules.setdefault(_namespace, _package)
    _sdk = import_module(_namespace + '.lite3_dev_server')
    DevClient = _sdk.DevClient
    DevClientError = _sdk.DevClientError

__all__ = ['DevClient', 'DevClientError']
