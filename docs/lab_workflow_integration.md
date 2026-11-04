# UPbRPy Studio in the lab workflow: database mode

**Status:** implemented 2026-11-03 (steps 1 to 5 of section 4: commits
44f9104, 8fcd4a7, cca3195, 21bf44f), verified end to end on macOS against a
local laicpmsdb with the full 197-spot `Apatite_20Mar25` session (link with
handoff code, fetch, auto-pick, reduce, submit, prefer; preferred dates equal
to laicpmsdb's own script reduction of the same bundle). The Windows
registration (`--register-url-scheme`) and a frozen-app launch by a real
`upbr://` click are not yet tried on a machine. Part of the cross-repository
contract `upbstudio/docs/WORKFLOW.md` v1 (sections 3.2, 3.3 and 4), which wins
where the two differ; laicpmsdb's `docs/workflow_api.md` gives the server's
exact shapes. `GUI/roadmap.md` keeps the architecture of Studio itself.

## 1. What changes and what does not

UPbRPy Studio stays a native PySide6 + pyqtgraph application, and the engine
in `UPbRPy/` stays free of Qt, HTTP and database code. Standalone use is
unchanged: open a Qtegra export directory or a `.upbr`, reduce, export the
reduction bundle. **Database mode** adds one optional way in and one way out:

- **In:** open a session from a laicpmsdb server. Studio fetches the
  *session bundle* (`raw/` CSVs byte-identical, `session_manifest.toml`, a
  pre-filled `.upbr` with an `origin` block) and opens it exactly as a local
  folder. It arrives through a link, `upbr://session/<id>?server=&code=`,
  from UPb Studio or a browser, or through **File > Open from database…**.
- **Out:** **Submit to database** posts the *reduction bundle* that
  `export_reduction_bundle` already writes to the session's origin server,
  and records the returned run id in the `.upbr`. **Mark as preferred** calls
  the server once more.

A user without the database can receive a session bundle as a zip and send a
reduction bundle back as a zip; the files are identical to what database mode
moves over HTTP.

## 2. Pieces

| Piece | Where | Notes |
| --- | --- | --- |
| `[database]` extra | `pyproject.toml` | `requests` (or `httpx`) and `keyring`; the GUI imports them lazily and shows the two database actions disabled with a one-line reason when the extra is missing |
| client | `UPbRPy/integration/laicpmsdb.py` | thin, typed, no Qt: `exchange_code`, `fetch_session_bundle`, `submit_reduction`, `prefer_run`, `whoami`; bearer token per server stored in the OS keychain (`keyring`, service `UPbRPy`), never in files; errors mapped to messages the GUI can show |
| `origin` in the project | `UPbRPy/project.py` | format version 2, additive: `origin { server, session_id, session_key, export_sha256, exported_at, run_id? }`; version 1 files still load |
| manifest provenance | `UPbRPy/pipeline.py` `manifest()` | `hash_inputs=True` by default in `export_reduction_bundle`; `reference_library_version` from `library.toml`; `metadata.origin` echoing the project's origin (laicpmsdb plan section 8.3) |
| session manifest | `UPbRPy/io.py` | `read_experiment_data` accepts `session_manifest.toml` beside `raw/` as the source of `sample_aliases`, `sep` and the reference-material plan; the GUI pre-fills from it |
| URL scheme | `GUI/main.py`, `packaging/UPbRPyStudio.spec`, installer | `--open` accepts a directory, a `.upbr`, a session-bundle zip, or a `upbr://` URL; macOS `CFBundleURLTypes` for `upbr` in the spec and a `QFileOpenEvent` handler; Windows registry entry `HKCU\Software\Classes\upbr` written by the installer (and offered on first run of a portable build); a second launch hands the URL to the running instance (local socket) |
| database mode UI | `GUI/` | a "Database" group in the File menu: Open from database… (server URL and sign-in: password, or ORCID through the system browser returning to `upbr://auth/callback?code=`), Submit to database, Mark as preferred; the origin shown in the window title and the project panel; a status row with the run id after a submit |
| cache | `~/UPbRPy/sessions/<session_key>/` | one folder per fetched session; the bundle hash tells Studio whether a re-fetch is needed; the user may move it |
| tests | `tests/` | a pytest fixture that serves a fake laicpmsdb (`http.server` in a thread) with the export, exchange, import and prefer routes; project format v2 round trip; `--open upbr://` parsing; manifest provenance |

## 3. The exchange

```text
browser or UPb Studio ── upbr://session/<id>?server=S&code=C ──> Studio
Studio ── POST S/v1/auth/exchange {code} ──> bearer token (keychain)
Studio ── GET  S/v1/sessions/<id>/export/upbrpy ──> zip ──> ~/UPbRPy/sessions/<key>/
Studio opens <key>/<key>.upbr (origin set) ── reduce locally ── export_reduction_bundle
Studio ── POST S/v1/sessions/<id>/runs/import (multipart zip) ──> {run_id, counts, quarantine}
Studio ── POST S/v1/sessions/<id>/runs/<run_id>/prefer ──> ok
```

Without `code`, Studio uses the keychain token for `S`, else asks to sign
in. Every request carries `User-Agent: UPbRPy-Studio/<version>`.

## 4. Order of work

1. Project format v2 with `origin`; manifest provenance; `session_manifest.toml`
   in the reader (engine only, no GUI).
2. The client module and the fake-server tests.
3. `--open` of `.upbr`, zips and `upbr://` URLs; the scheme registration in
   the PyInstaller spec and installer; single-instance hand-off.
4. The File > Database actions and the sign-in dialog.
5. End-to-end against a local laicpmsdb with the fixture session
   `Apatite_20Mar25`: open through the link, reduce, submit, prefer, and
   compare the preferred ages with the frozen publication tables.

Done when a session page in laicpmsdb launches Studio on a Mac and on
Windows, the reduction appears in the database, and the standalone test
suite still passes with the `[database]` extra absent.

## 5. Choices made while implementing

- `origin.export_sha256` is the SHA-256 of `session_manifest.toml` (the
  server's `X-Export-SHA256`), not of the zip, which is streamed and not
  reproducible. An equal hash keeps the cached folder, saved project
  included; a different one moves it aside as `<key>.superseded-<UTC time>`.
- `origin.server` is replaced by the base URL Studio actually used, since the
  keychain token belongs to that URL.
- The reduction zip has its files at the top level and includes
  `<session_key>.upbr`; a resubmission sends `supersedes=<previous run_id>`.
- `upbr://run/<id>` opens a cached `.upbr` whose `origin.run_id` matches, else
  asks `GET /v1/runs/{id}` for the session and fetches it.
- A session manifest's `sep` and `[samples]` win over a mineral preset's
  defaults in Studio (explicit project settings still win), and only `raw/*.csv`
  are hashed as inputs.
