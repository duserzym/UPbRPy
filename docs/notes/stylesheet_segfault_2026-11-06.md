# Segfaults in `QApplication.setStyleSheet` (2026-11-05 and 2026-11-06)

## What crashed

Eleven `Python-2026-11-0*.ips` reports are in `~/Library/Logs/DiagnosticReports/`,
including the three that were reported (`Python-2026-11-05-193824.ips`,
`Python-2026-11-05-195516.ips`, `Python-2026-11-06-100433.ips`). Every one of
them is an **offscreen test process**, not a running Studio:

- each loads `libqoffscreen.dylib` and `QtTest`;
- the parent process is `codex` in eight of them (the other three name a parent
  that had already exited or `launchd`);
- each process ran for one to five minutes before it crashed, as a full
  `pytest` run does.

Seven reports fault inside `QApplication.setStyleSheet`
(`Sbk_QApplicationFunc_setStyleSheet` then two stripped QtWidgets frames at
offsets `0xd4944` and `0xd47ac`, `EXC_BAD_ACCESS` at a small address such as
`0x40e` or `0x148`: a field read through a null or freed object). The others
fault in `QObject::~QObject` of a `QAbstractButtonWrapper` during
`DeferredDelete` (two), in `QTimerInfoList::activateTimers` from
`processEvents` (two), and in a PyQt `sipQMenu` destructor at interpreter exit
(one, a different binding: not UPbRPy code).

## Which tests

pytest enables `faulthandler`, so the Python stack of each crash is in the
Codex session logs (`~/.codex/sessions/2026/11/0{5,6}/`). The distinct stacks:

| Stack (innermost first) | Seen |
| --- | --- |
| `theme.apply_theme` (`app.setStyleSheet`) ← `theme.build_application` ← `GUI.main.main` ← `tests/test_studio_tabs.py::test_open_target_and_single_instance_handoff_add_tabs` | 2 |
| `theme.apply_theme` ← `tests/test_updates_gui.py::test_preferences_manual_feedback_and_theme_changes` (switch to dark) | 1 |
| `theme.apply_theme` ← `MainWindow.apply_theme_mode` ← `MainWindow.__init__` ← `StudioWindowController._create_reduction` ← `StudioWindow.add_tab` ← `StudioWindow.__init__` | 1 |
| `processEvents` in `tests/test_studio_tabs.py::wait_for` | 1 |
| `processEvents` in `tests/test_glass_theme.py::pump` | 1 |

## What the stacks have in common

- `apply_theme` calls `app.setStyleSheet` for the whole application, which
  unpolishes and repolishes every widget that exists. Before this change every
  `MainWindow` construction did so with an unchanged sheet, so each new tab
  restyled every widget in the process, including closed tabs and dialogs that
  wait for `deleteLater`.
- In a test session the process holds hundreds of widgets left by earlier
  tests. `test_open_target_and_single_instance_handoff_add_tabs` calls
  `main()` in-process, so `build_application` re-applies the sheet over all of
  them.
- Python code runs during the repolish: application-wide event filters
  (`_PopupCorners`, every `StudioWindow`, `VersionChip`, `UpdatesDialog`) see
  each `Polish` and `StyleChange` event. Python's cyclic garbage collector can
  run inside those calls and destroy Python-owned widgets while Qt iterates
  over them.

## What is and is not established

- Established: the crashes are in the test suite, intermittent (six crashed
  Codex sessions over two days of frequent full-suite runs), and the faulting
  call is the whole-application `setStyleSheet`.
- Not established: which widget is freed. The QtWidgets frames are stripped
  (no symbols for the static functions at `0xd47ac` / `0xd4944`), and a
  targeted reproduction (cyclic widgets created and dropped, GC forced during
  `setStyleSheet`) did not crash. The garbage-collector explanation is the
  working hypothesis. pyqtgraph does not appear in any faulting stack.
- No crash of a Studio that Yiming launched is on record.

## Guard (2026-11-06)

`GUI/theme.py` `apply_theme`:

- skips `app.setStyleSheet` when the application already has that exact sheet,
  so opening a tab no longer restyles every widget; a theme switch still does;
- pauses Python's cyclic garbage collector for the duration of
  `setStyleSheet` and `setPalette`, then restores its previous state.

`tests/test_stylesheet_stress.py` opens and closes tabs while switching themes
ten times.

If a crash in `setStyleSheet` recurs, record which test was running (the
faulthandler stack in the pytest output) and whether a Studio window was
closing at the time.
