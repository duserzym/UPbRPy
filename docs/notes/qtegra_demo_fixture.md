# RM-only Qtegra demo fixture

The public fixture is `tests/fixtures/qtegra/Zircon_demo/`. Its session name
is `Zircon_demo`; dates come from the acquisition metadata rather than a
file-name token. It contains a Rich CSV and 12 per-spot CSVs, with nine
channels and the source exports' metadata, cell text and line endings. The
Rich CSV has a UTF-8 BOM; the per-spot exports have none, as in the source.

The selected labels, in acquisition order, are:

- `NIST612_896`, `NIST612_897`, `NIST612_898`
- `NIST610_863`, `NIST610_864`, `NIST610_865`
- `S513_628`, `S513_629`
- `91500_738`, `91500_739`
- `PL_836`, `PL_837`

The last four are the first two 91500 and first two PL acquisitions in the
source session. They replace unpublished sample measurements and exercise
two secondary zircon reference materials. Tests needing a sample can treat
91500 as the sample without retaining unpublished measurements.

Regenerate with:

```bash
.venv/bin/python scripts/make_qtegra_fixture.py /path/to/zircon_session
```

The source argument is required. `--rich` selects a Rich file when discovery
is ambiguous. `--labels` selects acquisitions by label in source order;
`--channels`, `--name` and `--out` control the channel subset and output.
Non-reference labels require `--allow-non-reference`. Source files are read
only. Original positional `Sample N` header cells are retained, including
gaps in the source acquisition numbers.

The eight retained acquisitions' Rich cells and complete per-spot files
were compared byte for byte with the previous fixture. Every numeric
finalized result for those eight spots also compared exactly equal. The
checksums in `tests/test_make_qtegra_fixture.py` pin their original bytes.
Regeneration from the source session matched all 13 output files with `cmp`.

Expected values changed with the four acquisitions:

| Test | Previous expectation | Replacement | Reason |
| --- | --- | --- | --- |
| `test_qtegra.test_reader_counts_and_order` | Four unpublished labels after S513 | `91500_738`, `91500_739`, `PL_836`, `PL_837` | RM-only selection; counts stay 12 acquisitions, 9 channels, 72 sweeps |
| `test_qtegra.test_rich_metadata_and_per_channel_time` | Last spot numbers 323, 324, 325, 326 | 738, 739, 836, 837 | Source labels and laser-position metadata |
| `test_qtegra_experiment.test_file_source_uses_manifest_in_parent_with_existing_rules` | Former sample prefix after splitting on a hyphen | `91500_738` | The replacement label has no hyphen; manifest override and explicit underscore checks remain |
| `test_export_footnotes.test_compact_journal_prose_preserves_values_sources_and_clock` | Acquisition starts 15:55 to 16:01 | 15:55 to 16:20 | Replacement PL acquisitions occur later in the source session; instrument clock stays unchanged |
| `test_bias.test_demo_session_shared_pipeline` | Two S513 bias rows | Six bias rows: 91500 x2, PL x2, S513 x2 | Replacements are now recognized as reference materials |

No existing numerical tolerance was relaxed. The mean-session fixture
reduction gives 206Pb/238U dates of 1048.856015 and 1051.801173 Ma for 91500,
and 323.475338 and 322.856025 Ma for PL. These are within 5% of their
reference dates (about 1062 and 337 Ma); this small fixture has only two
primary S513 acquisitions and is not a full-session drift correction.
The bias test retains its existing absolute-offset threshold of 5% and
also pins the four replacement dates.

The narrow-layout tooltip test assigns the 91500 acquisitions to a demo
sample folder, keeping both tree categories populated and checking all 12
spot tooltips. Synthetic test-label substitutions elsewhere change names
only, without changing measurements, counts or tolerances.
