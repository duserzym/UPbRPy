# Export results

![Export results in UPbRPy Studio](../assets/screenshots/11-export-results.png)

## What this dialog does

Writes a publication table with explicit uncertainty branches and per-system
reference comparisons, with optional audit products for reproducibility.

## Controls

Choose **Export results…**, an output folder, file stem and formats. The
**Columns** tab selects groups or individual columns; **Defaults** restores
the default selection. Optional isotope concentrations and uncertainty columns
start unchecked; zircon-specific groups appear only for zircon.

**Settings** holds acquisition metadata, table title, discordance definition
and threshold, assumed silica/titania activities and CSV precision. Enter only
metadata supported by the acquisition record. Unavailable fields are identified
as “not recorded” in footnotes. Column and setting choices persist in the project.
**Preview** shows headings, sample summaries and generated footnotes.

The default products are `<stem>.xlsx`, `.csv` and `.md`. Excel keeps full
numeric precision with publication display formats, grouped headings and a
Reference materials sheet. CSV uses displayed values unless **CSV full
precision** is selected, and has companion sample-summary and footnote files.
Markdown contains sample blocks and numbered footnotes.

**Audit files and replay notebook** also writes the legacy publication workbook,
companion tables, selection windows, spot and depth results, reduction manifest
and a replay notebook. Calculation exclusions remain in the publication table
with an excluded note and are omitted from weighted means.

## What to check

Review the preview, reference assignments, exclusions, uncertainties and missing
metadata before writing. Retain audit products with the inputs. The public
screenshot uses the Columns tab so acquisition metadata is not displayed.
See the [table contract](../export_table_spec.md) and
[export data model](../data-model/export-table.md) for exact quantities.
