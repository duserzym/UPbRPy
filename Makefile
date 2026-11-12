MKDOCS ?= $(HOME)/Library/Caches/UPbRPy/docs-venv/bin/mkdocs
SITE_DIR ?= $(HOME)/Library/Caches/UPbRPy/docs-site

.PHONY: docs docs-serve test

docs:
	"$(MKDOCS)" build --strict --site-dir "$(SITE_DIR)"

docs-serve:
	"$(MKDOCS)" serve

# Tests run under a memory (3 GB) and time (10 min) watchdog; see scripts/guarded.sh.
test:
	UPBRPY_BLOCK_IMPORTS=requests,keyring QT_QPA_PLATFORM=offscreen scripts/guarded.sh -m 3072 -t 600 -- .venv/bin/python -m pytest tests -q -p no:cacheprovider

# scripts/build_desktop.py builds Studio under this folder (hash of the checkout path).
STUDIO_BUILD_ROOT := $(or $(UPBRPY_BUILD_ROOT),$(HOME)/Library/Caches/UPbRPy/$(shell printf %s "$(CURDIR)" | shasum -a 256 | cut -c1-12))

# After a major update (AGENTS.md): once Studio is installed in /Applications,
# drop the PyInstaller output. The build venv in the same folder is kept.
.PHONY: clean-build
clean-build:
	@case "$(STUDIO_BUILD_ROOT)" in ""|/|"$(HOME)"|"$(HOME)/") echo "Unexpected build folder '$(STUDIO_BUILD_ROOT)'; not cleaning."; exit 1;; esac
	@if pgrep -f "build_desktop.py|PyInstaller|pyinstaller" >/dev/null; then echo "A Studio build is running; not cleaning."; exit 1; fi
	@df -h / | awk 'NR==2 {print "Free on /: " $$4 " (before)"}'
	rm -rf ./build "$(STUDIO_BUILD_ROOT)/work" "$(STUDIO_BUILD_ROOT)/dist" "$(STUDIO_BUILD_ROOT)/previous-build" "$(STUDIO_BUILD_ROOT)/previous-dist"
	@df -h / | awk 'NR==2 {print "Free on /: " $$4 " (after)"}'
