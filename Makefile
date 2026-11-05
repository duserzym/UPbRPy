MKDOCS ?= $(HOME)/Library/Caches/UPbRPy/docs-venv/bin/mkdocs
SITE_DIR ?= $(HOME)/Library/Caches/UPbRPy/docs-site

.PHONY: docs docs-serve test

docs:
	"$(MKDOCS)" build --strict --site-dir "$(SITE_DIR)"

docs-serve:
	"$(MKDOCS)" serve

test:
	UPBRPY_BLOCK_IMPORTS=requests,keyring QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests -q -p no:cacheprovider
