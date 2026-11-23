PYTHON ?= python3
BROWSER ?= /Applications/Google Chrome.app/Contents/MacOS/Google Chrome

.PHONY: help check syntax syntax-legacy test browser-test demo

help:
	@echo "make check         Syntax compatibility and offline tests"
	@echo "make test          Standard-library test suite"
	@echo "make browser-test  Chrome smoke test (override BROWSER if needed)"
	@echo "make demo          Local synthetic camera demo"

check: syntax test

syntax:
	$(PYTHON) -c 'import ast; from pathlib import Path; [ast.parse(path.read_text(), feature_version=(3, 8)) for folder in ("scripts", "tests") for path in Path(folder).glob("*.py")]; print("PASS: Python 3.8 syntax for scripts and tests")'

# Python 3.13+ no longer documents support for the Python 3.6 grammar.
# CI runs this additional check with Python 3.8.
syntax-legacy:
	$(PYTHON) -c 'import ast, sys; from pathlib import Path; sys.version_info < (3, 13) or sys.exit("Use Python 3.8-3.12 for the legacy grammar check"); ast.parse(Path("scripts/lite3_camera_server.py").read_text(), feature_version=(3, 6)); print("PASS: server Python 3.6 syntax (not runtime validation)")'

test:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover -s tests -v

browser-test:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) tests/camera_browser_smoke.py --browser "$(BROWSER)"

demo:
	$(PYTHON) scripts/lite3_camera.py demo
