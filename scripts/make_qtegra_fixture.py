"""Build the RM-only Qtegra demo fixture from read-only instrument exports.

Usage::

    .venv/bin/python scripts/make_qtegra_fixture.py /path/to/zircon_session

The source folder must contain one Rich CSV (or select it with --rich) and
matching per-spot CSVs, directly or in subfolders. Select acquisitions by
label, then retain acquisition order. Cell text, metadata, BOMs and line
endings are preserved; output file names are neutral. Full exports stay
outside the repository.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_NAME = "Zircon_demo"
KNOWN_REFERENCE_MATERIALS = frozenset({"NIST612", "NIST610", "S513", "91500", "PL", "LTSE", "Seiland"})
DEFAULT_LABELS = (
    "NIST612_896", "NIST612_897", "NIST612_898",
    "NIST610_863", "NIST610_864", "NIST610_865",
    "S513_628", "S513_629", "91500_738", "91500_739", "PL_836", "PL_837",
)

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


def slice_single_csv(source: Path, target: Path, labels: list[str], channels: set[str]) -> None:
    """Keep selected acquisition columns and channel rows, including all metadata."""
    bom, lines, newline = read_lines(source)
    source_labels = lines[1].split(",")[4:-1]
    indices = [4 + source_labels.index(label) for label in labels]
    out = []
    for index, line in enumerate(lines):
        cells = line.split(",")
        if index >= 2 and cells[0] == "MainRuns" and cells[2] not in channels:
            continue
        out.append(",".join(cells[:4] + [cells[i] for i in indices] + [""]))
    write_lines(target, bom, out, newline)


def spot_files_by_label(folder: Path) -> dict[str, Path]:
    found = {}
    for path in sorted(folder.rglob("*.csv")):
        with path.open(encoding="utf-8-sig") as handle:
            match = SPOT_FIRST_LINE.match(handle.readline())
        if match:
            label = match.group("label")
            if label in found:
                raise ValueError(f"duplicate per-spot label: {label}")
            found[label] = path
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
    parser.add_argument("source", type=Path, help="folder holding the source exports")
    parser.add_argument("--rich", type=Path, help="source Rich CSV, relative to source or absolute")
    parser.add_argument("--name", default=DEFAULT_NAME, help="neutral output session name")
    parser.add_argument("--out", type=Path, help="output folder (default: tests/fixtures/qtegra/<name>)")
    parser.add_argument("--labels", default=",".join(DEFAULT_LABELS), help="comma-separated acquisition labels")
    parser.add_argument("--allow-non-reference", action="store_true", help="explicitly allow non-reference labels")
    parser.add_argument("--channels", default=",".join(DEFAULT_CHANNELS), help="comma-separated channels, or 'all'")
    args = parser.parse_args(argv)
    source = args.source.expanduser()
    if not source.is_dir():
        parser.error("source must be an existing folder")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", args.name):
        parser.error("name must contain only letters, digits, underscores and hyphens")
    candidates = [source / args.rich] if args.rich else sorted(source.rglob("*Rich.csv"))
    if len(candidates) != 1 or not candidates[0].is_file():
        parser.error("source must contain exactly one *Rich.csv; otherwise use --rich")
    rich = candidates[0]
    _bom, lines, _nl = read_lines(rich)
    source_labels = lines[1].split(",")[4:-1]
    requested = [label.strip() for label in args.labels.split(",") if label.strip()]
    if not requested or len(set(requested)) != len(requested):
        parser.error("labels must be nonempty and unique")
    if not args.allow_non_reference and any(
        label.rsplit("_", 1)[0] not in KNOWN_REFERENCE_MATERIALS for label in requested
    ):
        parser.error("non-reference labels require --allow-non-reference")
    if set(requested) - set(source_labels):
        parser.error("requested labels are missing from the Rich export")
    if any(source_labels.count(label) != 1 for label in requested):
        parser.error("requested labels must occur exactly once in the Rich export")
    labels = [label for label in source_labels if label in requested]
    available_channels = {line.split(",")[2] for line in lines[2:] if line.startswith("MainRuns,")}
    channels = available_channels if args.channels.strip() == "all" else {
        name.strip() for name in args.channels.split(",") if name.strip()
    }
    if not channels or channels - available_channels:
        parser.error("channels must be a nonempty subset of the source channels")
    spots = spot_files_by_label(source)
    if set(labels) - spots.keys():
        parser.error("missing matching per-spot exports")
    out = args.out or Path(__file__).resolve().parents[1] / "tests/fixtures/qtegra" / args.name
    if out.resolve() == source.resolve() or source.resolve() in out.resolve().parents:
        parser.error("output must be outside the read-only source folder")
    if out.exists() and any(out.iterdir()):
        expected = {f"{args.name}_Rich.csv", args.name.lower()}
        if {p.name for p in out.iterdir()} != expected:
            parser.error("refusing to overwrite an unrelated output folder")
    spot_out = out / args.name.lower()
    spot_out.mkdir(parents=True, exist_ok=True)
    for stale in spot_out.glob("*.csv"):
        stale.unlink()
    slice_single_csv(rich, out / f"{args.name}_Rich.csv", labels, channels)
    for index, label in enumerate(labels, 1):
        slice_spot_file(spots[label], spot_out / f"{args.name}_{index}.csv", channels)
    total = sum(path.stat().st_size for path in out.rglob("*") if path.is_file())
    print(f"wrote {len(labels)} acquisitions, {len(channels)} channels, {total / 1024:.0f} KiB to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
