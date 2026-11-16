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
