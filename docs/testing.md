# Testing

Run QUICK after every change:

```sh
make test-quick
```

Run FULL before building or installing Studio, and after changes to reduction
numerics, the `.upbr` format, database submit/link/preferred behavior,
`tests/conftest.py`, or `pyproject.toml`:

```sh
make test
```

QUICK compares working-tree and index changes with `HEAD`, including untracked
files. It normally selects test files using the functions they actually executed
and repository files they opened during the last completed FULL run. Python
changes under `GUI/`, `UPbRPy/`, and `scripts/` are mapped from both the old and
new diff line ranges to the innermost function or class qualname. Class body
changes select tests exercising that class or its methods. Module-level changes
(imports, constants, and module-level decorators) select tests executing
functions in that module. Each selected file prints the first changed function or data file that selected it.

A changed test file always runs in full, including its slow tests. Otherwise
QUICK uses `-m "not slow"` (individual tests taking more than about 20 s).
Changes to conftest, pyproject, or the runner select all files, with the same
slow test rule. Changed fixtures, reference-material TOML files, documentation,
and other data select the tests that opened them.

FULL refreshes the runtime map automatically, including completed runs with test
failures. Interrupted runs, missing worker reports, and collection mismatches
leave the previous map intact. The map is outside the repository:
`~/Library/Caches/UPbRPy/test-map/<hash>.json` on macOS, or
`${XDG_CACHE_HOME:-~/.cache}/UPbRPy/test-map/<hash>.json` elsewhere. The hash is
the first 12 hex characters of SHA-256 of the resolved checkout path. The file
stores the Git HEAD commit, Unix timestamp, and merged per-test-file function and
opened-file sets. If absent, older than 14 days, or recorded at a commit that is
not an ancestor of HEAD, QUICK prints a refresh reminder and uses the transitive
static import graph. A source module with no recorded functions also uses that
graph for just that module. Static fallback uses fixture-directory and data-model
documentation mentions for data dependencies.

Recording uses standard-library `sys.monitoring` (Python 3.12+) PY_START events
and an `open` audit hook. A C-level attribute router keeps ignored audit
events out of Python callbacks. Each code object is reported once per test file;
events restart at file boundaries. Collection imports and opens are attributed
to the file being collected. Worker JSON is enabled by `UPBRPY_TEST_MAP_DIR`;
FULL sets it for each chunk. The summary reports time spent in recording
callbacks and file-boundary restarts as a fraction of summed worker session
time. This measures callback work, not interpreter event-dispatch overhead.

QUICK is a feedback tool, not a complete dependency proof. It can miss tests
never run before, branches not exercised by the recorded FULL, cached data read
only while another file was being collected, and Qt C++ behavior. Code reached
through threads or child processes is recorded only when it executes inside the
instrumented test process; separate child interpreters are not traced. In
particular the restore child process is not traced. Use FULL when those limits
matter. New or changed tests always run even without prior map entries.

FULL runs every test, including slow tests. Both tiers use independent pytest
processes, without pytest-xdist. The default worker count is the smaller of 8
and the CPU count minus 4, with a minimum of 1. Files heavier than half the
expected per-worker load are split by collection position modulo shard count.
Small files are grouped into chunks of about 20–30 s. Chunks run largest first
on a dynamic queue. A separate unsharded collection verifies exact node coverage.
Files containing `isolated` tests run one at a time after the parallel queue
drains. These tests assert wall-clock UI latency; concurrent CPU load can exceed
their limits. Their assertions and limits remain unchanged.
Each process uses offscreen Qt, temporary Qt settings and autosave folders, and
blocks requests/keyring imports unless `UPBRPY_BLOCK_IMPORTS` is already set.

`make test` uses the watchdog with a 12,288 MB memory limit and 900 s time limit.
`make test-serial` preserves the previous single-process command and its
3,072 MB / 600 s limits. The measured serial suite took 1,025 s, so the old
watchdog can stop it before completion.

For a different worker count, a different comparison base, or a plan without
running tests:

```sh
.venv/bin/python scripts/run_tests.py full --workers 6
.venv/bin/python scripts/run_tests.py quick --since HEAD~1
.venv/bin/python scripts/run_tests.py quick --list
```

To exercise selection without modifying files, replace Git discovery with an
explicit list (a whole source file, a line number, or a code qualname):

```sh
.venv/bin/python scripts/run_tests.py quick --changed UPbRPy/age.py:calc_76_date
.venv/bin/python scripts/run_tests.py quick --changed GUI/widgets.py:ResultsTableWidget.show_results
.venv/bin/python scripts/run_tests.py quick --changed GUI/main_window.py:100 --list
```

Refresh the committed per-file weights from pytest `--durations=0` output:

```sh
.venv/bin/python scripts/run_tests.py --update-durations /tmp/durations.txt
```

Or use the JUnit XML from the last complete FULL run in this checkout:

```sh
.venv/bin/python scripts/run_tests.py --update-durations
```

Updating durations runs no tests. Text durations sum setup, call and teardown
rows by file; files absent from the text receive 1 s. JUnit durations include
those phases by default. Timing files contain seconds, rounded to 0.01 s.

Each run prints selected files, chunk progress, JUnit totals and wall time.
Failures print node IDs and the last 25 lines of their captured output. Logs,
collection manifests and JUnit files remain in the printed temporary directory,
including failed chunks. The latest FULL run's location is recorded in a small
checkout-specific file in the system temporary directory. Missing reports,
collection mismatches, pytest failures and subprocess errors return status 1.
