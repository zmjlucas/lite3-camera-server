"""Import one camera dependency in an isolated process and print its version."""
import importlib
import sys


module = importlib.import_module(sys.argv[1])
print(getattr(module, "__version__", "import OK"))
