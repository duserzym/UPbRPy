# Contributing and bug reports

Report a reproducible problem at [GitHub Issues](https://github.com/duserzym/UPbRPy/issues).
Include the Studio version, operating system, source format, mineral preset,
affected step, expected behaviour and actual behaviour. A traceback is useful
when it can be shared without private metadata.

Provide a small reference-material-only reproduction when possible. Do not
publish analytical sessions, unknown sample identities, personal metadata,
server credentials or local paths. Screenshots should show neutral labels and
no acquisition metadata. The repository's small Qtegra fixture is suitable for
reader and documentation tests; full instrument exports remain local.

Read `AGENTS.md` and the [Qtegra contract](../qtegra_export_formats.md) before
changing readers. Keep engine modules free of GUI and network dependencies.
Add focused tests under `tests/` and keep the [data model](../data-model/index.md)
consistent with reader or writer changes. Run [QUICK](../testing.md) after
changes, and FULL for numerical or persistence changes and before building Studio.
