# In-app updates

Studio uses the shared `GUI/labupdate/` package to check the latest stable
release, verify its minisign signature, and offer **Restart to update**.
`GUI/update_config.py` supplies Studio's identity, embedded public key,
`UPbRPy.tls.urlopen`, and bundled release notes. `GUI/updater.py` retains the
independent standards check and its timer.

## Mechanism

The feed is
`https://github.com/duserzym/UPbRPy/releases/latest/download/latest.json`.
It contains a version, brief `notes`, and Tauri platform entries with an HTTPS
archive URL and signature. Studio checks after eight seconds and every six
hours. Automatic downloads are on by default. Checks and downloads run in
workers with a socket stall timeout and size limits. Studio uses the system
trust store through the engine's TLS helper. A stored Beta preference migrates
to stable silently; there is no channel selector.

Archives are verified before download completion and again before installation.
The installer validates archive paths and symlinks, stages beside the current
app, and launches a detached helper. The helper waits for Studio to exit,
retains the previous installation, moves the new one into place, and relaunches
it. The backup is removed on the next successful startup. Source checkouts and
unwritable installations show a reason and a GitHub download link.

The status-bar circular-arrows button opens the update popover. Its badge marks
an available or ready update, and it rotates while busy when animation is
allowed. The canvas chip always shows `v<current>` and opens Studio's Updates
dialog, which also keeps its standards section. Help > Check for updates opens
that dialog and requests a check. Errors show a brief classified message and a
Details line limited to 120 characters.

Available and ready states show the new version's notes in both views, limited
to four rendered lines, with a **Release notes** link to its version tag.
Before installing, the updater saves `updates/pending_version` and
`updates/pending_notes`. On startup it compares `updates/last_seen_version`
with the running version. An upgrade shows a **What's new in v<current>** card
once, including after a manual reinstall. First installation only records the
version. Notes come from the bundled file, matching pending notes, or
`Updated to v<current>.` The card closes with ✕ or Escape; the popover's
**What's new** link reopens it.

## Release flow

1. Write `release-notes/<version>.md` at the repository root. Use plain text or
   at most five `- ` bullets, no headings, no more than 600 characters. Describe
   changes for lab users. The feed builder refuses missing, empty or oversized
   notes. Both source packages and frozen apps include these files.
2. Run FULL tests, then build and sign the application. Apple code signing and
   updater minisign signing are separate. See [Build Studio](development/building.md).
3. Build archives with `--archive --sign-with /path/to/updater.key` (or set
   `UPBRPY_UPDATER_KEY`). The embedded public key must match that private key.
   Upload the ZIP, `.app.tar.gz`, `.sig`, `latest.json`, and `SHA256SUMS.txt`
   together to the matching stable version release.
4. Publish the release after verifying a frozen download and restart on a
   writable installation. Keep the legacy ZIP and checksum entry: installed
   0.1.3–0.1.5 copies still discover those through their GitHub API updater.

The existing CI triggers remain version tags and manual dispatch. Its ZIP-only
artifact/publish steps do not provision a signing key or publish the signed
feed. The owner must supply signing and upload the additional assets before
expecting new clients to update. Never publish private signing keys.

The portable feed builder can combine macOS and Windows assets:

```sh
python -m GUI.labupdate.release --repo duserzym/UPbRPy --version 0.1.5 \
  --notes-file release-notes/0.1.5.md \
  --darwin-aarch64 /path/UPbRPy-Studio-0.1.5-macOS-arm64.app.tar.gz \
  --windows-x86_64 /path/UPbRPy-Studio-0.1.5-Windows-x86_64.zip \
  --pubkey-file /path/upbrpy-studio-updater.key.pub \
  --sign-with /path/upbrpy-studio-updater.key --out /path/latest.json
```

It signs missing `.sig` files with `cargo tauri signer sign -f KEY -p "" PATH`
and an empty `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`. It verifies every signature
against the public key before writing a feed. Existing signatures are verified,
never silently replaced. This build-only module is not imported at runtime.

## Checks

`tests/test_labupdate_*.py` cover signatures, safe installation, TLS, states,
release feeds, Qt controls, and the disposable-key self-test.
`tests/test_updates.py`, `tests/test_updates_gui.py`, and
`tests/test_build_desktop.py` cover Studio's injected transport, automatic and
manual downloads, pending notes, first-run/upgrade notes, standards checks,
small-screen controls, backward-compatible archives, and build diagnostics.

The frozen smoke test runs `--update-self-test <file://manifest>
--update-self-test-output <report.json>` with missing default CA paths and
Homebrew certificates hidden where supported. It verifies a signed local
payload with the embedded disposable self-test key, rejects a tampered vector,
checks the production public-key format, and checks HTTPS to `github.com`.
Only an offline reachability warning is allowed. This path never installs an
archive or accepts the self-test key in the production updater.

`Updater(preview=...)` and `UPBRPY_UPDATE_PREVIEW` support all nine canonical
states without network or installation. Real macOS/Windows replacement and
relaunch still require a packaged-app check on each target platform.
