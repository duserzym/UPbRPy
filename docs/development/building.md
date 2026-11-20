# Build Studio

The PyInstaller recipe bundles Studio, the engine, reference-material TOML files
and icons. Build outputs stay outside the source checkout.

```sh
python -m pip install -e ".[gui,build,database]"
scripts/guarded.sh -m 12288 -t 900 -- .venv/bin/python scripts/build_desktop.py --archive
```

The build runs an offscreen frozen-application smoke test by default and prints
the application, screenshot and ZIP locations. `--dist-dir` and `--work-dir`
select external locations; paths inside the repository are rejected.
`UPBRPY_BUILD_ROOT` overrides the default cache root. The macOS default is
`~/Library/Caches/UPbRPy/<checkout-id>/`; Linux uses the XDG cache directory and
Windows the local application-data directory. macOS produces
`dist/UPbRPy Studio.app`; Windows produces an application folder with its executable.

`APPLE_SIGNING_IDENTITY` selects a macOS certificate. Without it, PyInstaller's
ad-hoc signature remains. Signing and Apple notarization are separate steps;
the distributed macOS build uses the lab certificate and is not notarized.

To verify a frozen executable against a local session, use
`--verify-reduction /path/to/session --verification-output /path/to/scratch`.
It writes the reduction bundle and a verification record, reports export
errors through its exit code, and records whether the runtime was frozen.
Keep analytical inputs and output artifacts outside the repository.

Run [FULL tests](../testing.md) before a build or installation. The desktop
workflow builds on version tags and manual dispatch, smoke-tests the frozen
application, and archives it. See [release flow](../in-app-updates.md).

After installing and smoke-testing the built application, note disk space with
`df -h /` and run `make clean-build`. It removes disposable build outputs while
keeping the build environment and installed app. Do not clean a running build.

## Signed update assets and legacy copies

On macOS arm64, `--archive` keeps the exact legacy
`UPbRPy-Studio-<version>-macOS-arm64.zip` and also creates
`UPbRPy-Studio-<version>-macOS-arm64.app.tar.gz`. The tarball contains the signed
`.app` with symlinks preserved. `SHA256SUMS.txt` lists each generated release
asset (the checksum file does not list itself). Installed 0.1.3–0.1.5 copies
continue to use the legacy ZIP plus its SHA-256 entry.

Pass `--sign-with /path/to/updater.key`, or set `UPBRPY_UPDATER_KEY`, to create
the tarball's `.sig` and a verified Tauri `latest.json` through
`GUI.labupdate.release`. Install cargo-tauri in the build environment before
signing. The private key must match Studio's embedded public key. The builder
reads `release-notes/<version>.md`; it refuses missing, empty or over-600-character
notes. Publish all assets together as described in [In-app updates](../in-app-updates.md).
The version is not inferred from the key or changed by archiving.

The frozen smoke test also writes a local `file://` update manifest and runs
`--update-self-test MANIFEST_URL --update-self-test-output FILE`. The local
payload is signed with the shared self-test key. A successful report requires
signature verification, rejection of tampered data, manifest/archive
verification, a valid production key, and working TLS trust. The network check
to `github.com` may report an offline warning. Like `--check-tls`, this step runs
with missing default CA paths and Homebrew certificates hidden on macOS where
`sandbox-exec` is available. A real signed build must still be installed and
smoke-tested for download, replacement, and relaunch by the supervisor.
