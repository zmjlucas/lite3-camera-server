PYTHON ?= python3
BROWSER ?=

.PHONY: help check syntax syntax-device test browser-test sdk-check browser-client-test

help:
	@echo "make check         Run syntax checks and offline tests"
	@echo "make test          Run unit tests with the Python standard library"
	@echo "make browser-test  Run the Chrome playback test (set BROWSER to select a browser)"
	@echo "make sdk-check     Build and validate the installed SDK (install .[dev] first)"

check: syntax test

syntax:
	$(PYTHON) -B scripts/check.py --syntax-only --shell

# Python 3.13+ no longer documents support for the Python 3.6 grammar.
# CI runs this check with Python 3.8.
syntax-device:
	$(PYTHON) -B scripts/check.py --device-syntax

test:
	$(PYTHON) -B -m unittest discover -s tests -v

sdk-check:
	$(PYTHON) -B scripts/check_distribution.py

browser-client-test:
	node tests/dev_client_test.js

browser-test:
	$(PYTHON) -B tests/camera_browser_smoke.py $(if $(BROWSER),--browser "$(BROWSER)")
	$(PYTHON) -B tests/camera_sources_browser_smoke.py $(if $(BROWSER),--browser "$(BROWSER)")
	$(PYTHON) -B tests/dev_browser_smoke.py $(if $(BROWSER),--browser "$(BROWSER)")
