"""Cut a small test fixture from the three Qtegra exports of one session.

The Zircon_demo session (Boise iCAP RQ, Qtegra
2.10.4345.64) was exported three ways: the Rich single CSV, the UPbR single
CSV, and a folder of per-spot CSV files. This script copies the first ``n``
acquisitions of all three, restricted to a subset of channels so the fixture
stays under 1 MB, into ``tests/fixtures/qtegra/<name>/``:

    Zircon_04Nov26_slice_Rich.csv     Rich single CSV, n sample columns
    Zircon_04Nov26_slice_UPbR.csv     UPbR single CSV, n sample columns
    zircon_04Nov26/                   the n matching per-spot CSV files

Cells are copied as text, byte for byte (BOM and CRLF line endings kept), so
the UPbR slice stays exactly the Y rows of the Rich slice and the per-spot
values stay equal to the Rich Y values. Only whole columns (acquisitions and
channels) are dropped; no value is reformatted. The source files are read
only; the full exports never go into git.

Usage::

    python scripts/make_qtegra_fixture.py \\
        "/path/to/LAICPMS_data"
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

RICH_NAME = "Zircon_demo_Rich.csv"
UPBR_NAME = "Zircon_demo_UPbR.csv"
FOLDER_NAME = "zircon_04Nov26"
SLICE_PREFIX = "Zircon_04Nov26_slice"

#: Channels kept in the fixture: the U, Th and Pb channels with 204Hg, the matrix
#: channels Si and Zr, and Hf; Si, Zr and Hf saturate the pulse counter in this
#: slice (so the xcal factor can be recovered from Y / Analog).
DEFAULT_CHANNELS = (
    "29Si", "91Zr", "177Hf", "204Hg",
    "206Pb", "207Pb", "208Pb", "232Th", "238U",
)

SPOT_FIRST_LINE = re.compile(r"^(?P<label>.+):(?P<time>\d\d/\d\d/\d{4} \d\d:\d\d:\d\d [AP]M);")
SPOT_HEADER_LINE = 13  # zero-based index of "Time,29Si,..."
SPOT_DWELL_LINE = 14   # zero-based index of ",dwell time=...;xcal factor=...,"


def read_lines(path: Path) -> tuple[str, list[str], str]:
    """Return (BOM, lines without terminators, line terminator)."""
    raw = path.read_bytes().decode("utf-8")
    bom = "\ufeff" if raw.startswith("\ufeff") else ""
    raw = raw[len(bom):]
    newline = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.split(newline)
    if lines and lines[-1] == "":
        lines.pop()
    return bom, lines, newline


def write_lines(path: Path, bom: str, lines: list[str], newline: str) -> None:
    path.write_bytes((bom + newline.join(lines) + newline).encode("utf-8"))


def slice_single_csv(source: Path, target: Path, n: int, channels: set[str]) -> list[str]:
    """Keep the first ``n`` sample columns and the MainRuns rows of ``channels``."""
    bom, lines, newline = read_lines(source)
    out = []
    labels: list[str] = []
    for index, line in enumerate(lines):
        cells = line.split(",")
        if index >= 2 and cells[0] == "MainRuns" and cells[2] not in channels:
            continue
        # Four key columns, n acquisition columns, and the trailing empty
        # cell that every row of the export ends with.
        kept = cells[: 4 + n] + [""]
        if index == 1:
            labels = cells[4 : 4 + n]
        out.append(",".join(kept))
    write_lines(target, bom, out, newline)
    return labels


def spot_files_by_label(folder: Path) -> dict[str, Path]:
    found = {}
    for path in sorted(folder.glob("*.csv")):
        with path.open(encoding="utf-8") as handle:
            match = SPOT_FIRST_LINE.match(handle.readline())
        if match:
            found[match.group("label")] = path
    return found


def slice_spot_file(source: Path, target: Path, channels: set[str]) -> None:
    """Keep the Time column and ``channels`` in one per-spot file."""
    bom, lines, newline = read_lines(source)
    header = lines[SPOT_HEADER_LINE].split(",")
    keep = [0] + [i for i, name in enumerate(header) if name in channels]
    out = lines[:SPOT_HEADER_LINE]
    for line in lines[SPOT_HEADER_LINE:]:
        if line == "":
            out.append(line)
            continue
        cells = line.split(",")
        out.append(",".join([cells[i] for i in keep] + [""]))
    write_lines(target, bom, out, newline)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="folder holding the three exports")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "tests" / "fixtures" / "qtegra" / SLICE_PREFIX,
    )
    parser.add_argument("-n", type=int, default=12, help="number of acquisitions")
    parser.add_argument(
        "--channels",
        default=",".join(DEFAULT_CHANNELS),
        help="comma-separated channels to keep, or 'all'",
    )
    args = parser.parse_args(argv)
    source = args.source.expanduser()
    out = args.out
    channels_arg = args.channels.strip()

    rich = source / RICH_NAME
    upbr = source / UPBR_NAME
    folder = source / FOLDER_NAME
    for path in (rich, upbr, folder):
        if not path.exists():
            parser.error(f"missing source: {path}")
    if channels_arg == "all":
        _bom, lines, _nl = read_lines(rich)
        channels = {line.split(",")[2] for line in lines[2:] if line.startswith("MainRuns,")}
    else:
        channels = {name.strip() for name in channels_arg.split(",") if name.strip()}

    spot_out = out / FOLDER_NAME
    spot_out.mkdir(parents=True, exist_ok=True)
    for stale in spot_out.glob("*.csv"):
        stale.unlink()

    labels = slice_single_csv(rich, out / f"{SLICE_PREFIX}_Rich.csv", args.n, channels)
    upbr_labels = slice_single_csv(upbr, out / f"{SLICE_PREFIX}_UPbR.csv", args.n, channels)
    if labels != upbr_labels:
        raise SystemExit(f"Rich and UPbR labels differ: {labels} vs {upbr_labels}")

    spots = spot_files_by_label(folder)
    for label in labels:
        if label not in spots:
            raise SystemExit(f"no per-spot file for {label}")
        source_file = spots[label]
        slice_spot_file(source_file, spot_out / source_file.name, channels)

    total = sum(path.stat().st_size for path in out.rglob("*") if path.is_file())
    print(f"wrote {len(labels)} acquisitions, {len(channels)} channels, {total / 1024:.0f} KiB to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
