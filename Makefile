PYTHON ?= python3
BROWSER ?= /Applications/Google Chrome.app/Contents/MacOS/Google Chrome

.PHONY: help check syntax syntax-legacy test browser-test

help:
	@echo "make check         Run syntax checks and offline tests"
	@echo "make test          Run unit tests with the Python standard library"
	@echo "make browser-test  Run the Chrome playback test (set BROWSER to select a browser)"

check: syntax test

syntax:
	$(PYTHON) -c 'import ast; from pathlib import Path; [ast.parse(path.read_text(), feature_version=(3, 8)) for folder in ("src", "scripts", "tests") for path in Path(folder).rglob("*.py")]; print("PASS: Python 3.8 syntax for source, scripts, and tests")'
	@for script in src/remote/*.sh tests/fixtures/*.sh; do bash -n "$$script" || exit; done

# Python 3.13+ no longer documents support for the Python 3.6 grammar.
# CI runs this check with Python 3.8.
syntax-legacy:
	$(PYTHON) -c 'import ast, sys; from pathlib import Path; sys.version_info < (3, 13) or sys.exit("Use Python 3.8-3.12 to check Python 3.6 syntax"); [ast.parse(path.read_text(), feature_version=(3, 6)) for path in [Path("src/server.py"), *Path("src/remote").glob("*.py")]]; print("PASS: server and remote helper Python 3.6 syntax. Runtime operation remains unverified.")'

test:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover -s tests -v

browser-test:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) tests/camera_browser_smoke.py --browser "$(BROWSER)"
