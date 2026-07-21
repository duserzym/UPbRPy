#!/usr/bin/env python3
"""Convert an iolite JSON reference-material directory to UPbRPy TOML.

The importer intentionally creates a new ``IOLITE_*`` standard for every JSON
file.  It never merges an imported record into a curated publication record,
so differing source compilations remain independently inspectable.  Elemental
concentrations are assigned to the isotope channels normally measured by the
UPbRPy method; the conversion convention is stated in each group's note.

Examples
--------
python scripts/import_iolite_reference_materials.py \
    "/path/to/iolite/Reference materials" UPbRPy/reference_materials
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import OrderedDict
from pathlib import Path


# These are channel labels, not abundance corrections: a reported elemental
# mass fraction remains unchanged when it is associated with a measured isotope.
ELEMENT_CHANNELS = {
    "H": "1H",
    "Li": "7Li",
    "Be": "9Be",
    "B": "11B",
    "C": "13C",
    "F": "19F",
    "Na": "23Na",
    "Mg": "24Mg",
    "Al": "27Al",
    "Si": "29Si",
    "P": "31P",
    "S": "34S",
    "Cl": "35Cl",
    "K": "39K",
    "Ca": "44Ca",
    "Sc": "45Sc",
    "Ti": "49Ti",
    "V": "51V",
    "Cr": "52Cr",
    "Mn": "55Mn",
    "Fe": "57Fe",
    "Co": "59Co",
    "Ni": "60Ni",
    "Cu": "63Cu",
    "Zn": "66Zn",
    "Ga": "69Ga",
    "Ge": "73Ge",
    "As": "75As",
    "Se": "77Se",
    "Br": "79Br",
    "Rb": "85Rb",
    "Sr": "88Sr",
    "Y": "89Y",
    "Zr": "91Zr",
    "Nb": "93Nb",
    "Mo": "95Mo",
    "Ru": "101Ru",
    "Rh": "103Rh",
    "Pd": "105Pd",
    "Ag": "107Ag",
    "Cd": "111Cd",
    "In": "115In",
    "Sn": "118Sn",
    "Sb": "121Sb",
    "Te": "125Te",
    "Cs": "133Cs",
    "Ba": "137Ba",
    "La": "139La",
    "Ce": "140Ce",
    "Pr": "141Pr",
    "Nd": "146Nd",
    "Sm": "147Sm",
    "Eu": "153Eu",
    "Gd": "157Gd",
    "Tb": "159Tb",
    "Dy": "163Dy",
    "Ho": "165Ho",
    "Er": "166Er",
    "Tm": "169Tm",
    "Yb": "172Yb",
    "Lu": "175Lu",
    "Hf": "177Hf",
    "Ta": "181Ta",
    "W": "182W",
    "Re": "185Re",
    "Os": "189Os",
    "Ir": "193Ir",
    "Pt": "195Pt",
    "Au": "197Au",
    "Hg": "202Hg",
    "Tl": "205Tl",
    "Pb": "208Pb",
    "Bi": "209Bi",
    "Th": "232Th",
    "U": "238U",
}

MATRIX_DIRECTORIES = {
    "apatite": "apatite",
    "caco3": "carbonate",
    "calcite": "carbonate",
    "glass": "glass",
    "hematite": "hematite",
    "unspecified": "unspecified",
    "zircon": "zircon",
}

SOURCE_IDS = {
    "A_MAD.json": "thomson2012",
    "A_McClure.json": "schoene_bowring2006",
    "C_MMC.json": "iolite_local_shells",
    "G_BCR2G.json": "georem2005",
    "G_BHVO2G.json": "georem2005",
    "G_GSD1G.json": "georem2005",
    "G_NIST610.json": "georem2005",
    "G_NIST612.json": "georem2005",
    "H_HFO.json": "iolite_hfo_liam",
    "Z_91500.json": "iolite_91500_compilation",
    "Z_GJ1.json": "iolite_gj1_compilation",
    "Z_Maniitsoq.json": "iolite_maniitsoq_personal_communication",
    "Z_OG1.json": "stern2009",
    "Z_Plesovice.json": "slama2008",
    "Z_Temora2.json": "iolite_temora2_compilation",
}

RADIOGENIC_RATIOS = {
    "206Pb/238U",
    "207Pb/235U",
    "208Pb/232Th",
    "207Pb/206Pb",
    "206Pb/208Pb",
    "238U/206Pb",
}


def _quoted(value: str) -> str:
    """Return a UTF-8 TOML basic string."""
    return json.dumps(value, ensure_ascii=False)


def _number(value: float | int) -> str:
    """Render finite JSON numbers without introducing binary rewrites."""
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"Non-finite iolite value: {value!r}")
    if isinstance(value, int):
        return str(value)
    return repr(number)


def _slug(value: str, *, upper: bool = False) -> str:
    """Create a filesystem- and identifier-safe representation."""
    value = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_") or "EMPTY_FILENAME"
    return value.upper() if upper else value.lower()


def _source_id(filename: str) -> str:
    """Resolve publication provenance, falling back explicitly when absent."""
    return SOURCE_IDS.get(filename, "iolite_unattributed")


def _matrix_directory(raw_matrix: str) -> str:
    """Map iolite's free-text matrix label to a stable directory name."""
    return MATRIX_DIRECTORIES.get(str(raw_matrix).strip().lower(), "unspecified")


def _is_u_th_pb_mineral(matrix: str, description: str) -> bool:
    """Return whether unmarked U-Th-Pb ratios represent radiogenic values."""
    text = f"{matrix} {description}".lower()
    return any(name in text for name in ("apatite", "calcite", "hematite", "zircon"))


def _classify_result(
    result: dict,
    matrix: str,
    description: str,
) -> tuple[str, str, str, float]:
    """Return normalized quantity, role, unit, and numeric scale factor."""
    raw_name = str(result.get("Name", "")).strip()
    raw_unit = str(result.get("Units", "")).strip().lower()

    if raw_name == "Age":
        return "concordia_age", "reference_age", "Ma", 1.0
    if raw_name.startswith("d"):
        return raw_name, "delta_value", "permil", 1.0
    if raw_name.lower().startswith("ehf"):
        return raw_name, "epsilon_value", "dimensionless", 1.0
    if "/" in raw_name or raw_name == "176Hf177Hf":
        normalized = raw_name.replace("176Hf177Hf", "176Hf/177Hf")
        if normalized.startswith("c"):
            return normalized[1:] + "_i", "initial_ratio", "ratio", 1.0
        if normalized.endswith("(i)"):
            return normalized[:-3] + "_i", "initial_ratio", "ratio", 1.0
        is_radiogenic = normalized in RADIOGENIC_RATIOS
        if is_radiogenic and _is_u_th_pb_mineral(matrix, description):
            return normalized + "*", "radiogenic_ratio", "ratio", 1.0
        return normalized, "isotope_ratio", "ratio", 1.0

    channel = ELEMENT_CHANNELS.get(raw_name, raw_name)
    scale = 10_000.0 if raw_unit == "wtpc" else 1.0
    return channel, "concentration", "ug/g", scale


def _append_group(
    lines: list[str],
    standard_id: str,
    group_key: tuple[str, str, str],
    values: OrderedDict[str, dict],
) -> None:
    """Append one readable array-of-tables measurement group."""
    role, unit, source = group_key
    lines.extend(
        [
            f'[[standards.{_quoted(standard_id)}.measurements]]',
            f"role = {_quoted(role)}",
            f"unit = {_quoted(unit)}",
            'status = "provisional"',
            f"source = {_quoted(source)}",
            (
                'notes = "Imported from an iolite JSON reference-material record. '
                "Element concentrations are assigned to UPbRPy isotope channels; "
                "the values are elemental mass fractions, not "
                'isotope-abundance-scaled."'
                if role == "concentration"
                else (
                    'notes = "Imported without numerical reinterpretation from '
                    'an iolite JSON reference-material record."'
                )
            ),
            "",
            f'[standards.{_quoted(standard_id)}.measurements.values]',
        ]
    )
    for quantity, metadata in values.items():
        value_text = _number(metadata["value"])
        uncertainty = metadata.get("uncertainty")
        if uncertainty is None:
            rendered = value_text
        else:
            rendered = (
                f"{{ value = {value_text}, uncertainty = {_number(uncertainty)}, "
                'uncertainty_type = "reported_unspecified" }'
            )
        lines.append(f"{_quoted(quantity)} = {rendered}")
    lines.append("")


def convert_iolite_file(path: Path, output_root: Path) -> tuple[str, int]:
    """Convert one iolite JSON file and return its manifest path and row count."""
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    properties = document.get("Properties") or {}
    matrix = str(properties.get("Matrix") or "unspecified").strip()
    description = str(properties.get("Description") or "").strip()
    original_name = str(properties.get("Name") or "").strip()
    filename_label = "EMPTY_FILENAME" if path.name == ".json" else path.stem
    standard_id = "IOLITE_" + _slug(filename_label, upper=True)
    directory = _matrix_directory(matrix)
    destination = output_root / directory / f"iolite_{_slug(filename_label)}.toml"
    destination.parent.mkdir(parents=True, exist_ok=True)

    aliases = []
    for alias in (original_name, path.stem if path.name != ".json" else ""):
        if alias and alias != standard_id and alias not in aliases:
            aliases.append(alias)
    raw_source = str(properties.get("Data Source") or "").strip()
    origin = f"iolite:{path.name}"
    if raw_source:
        origin += f"; Data Source: {raw_source}"

    groups: OrderedDict[tuple[str, str, str], OrderedDict[str, dict]] = OrderedDict()
    duplicate_count = 0
    for result in document.get("Results") or []:
        quantity, role, unit, scale = _classify_result(result, matrix, description)
        raw_value = result.get("Value")
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise ValueError(f"{path.name}/{quantity} has a nonnumeric value")
        value = float(raw_value) * scale
        raw_uncertainty = result.get("Uncertainty")
        uncertainty = None
        if (
            isinstance(raw_uncertainty, (int, float))
            and not isinstance(raw_uncertainty, bool)
            and float(raw_uncertainty) != 0.0
        ):
            uncertainty = abs(float(raw_uncertainty) * scale)
        key = (role, unit, _source_id(path.name))
        group = groups.setdefault(key, OrderedDict())
        candidate = {"value": value, "uncertainty": uncertainty}
        if quantity in group:
            if group[quantity] == candidate:
                duplicate_count += 1
                continue
            raise ValueError(f"{path.name} contains conflicting values for {quantity}")
        group[quantity] = candidate

    lines = [
        (
            "# Imported iolite reference-material record; kept separate from "
            "curated records."
        ),
        "format_version = 2",
        f"material = {_quoted(directory)}",
        "",
        f"[standards.{_quoted(standard_id)}]",
        "aliases = [" + ", ".join(_quoted(alias) for alias in aliases) + "]",
        f"description = {_quoted(description or f'iolite matrix: {matrix}')}",
        f"origin = {_quoted(origin)}",
        "",
    ]
    for group_key, values in groups.items():
        _append_group(lines, standard_id, group_key, values)
    destination.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return destination.relative_to(output_root).as_posix(), duplicate_count


def convert_directory(input_root: Path, output_root: Path) -> tuple[list[str], int]:
    """Convert every top-level JSON file in a deterministic filename order."""
    manifest_paths = []
    duplicate_count = 0
    for path in sorted(
        input_root.glob("*.json"),
        key=lambda item: item.name.casefold(),
    ):
        manifest_path, duplicates = convert_iolite_file(path, output_root)
        manifest_paths.append(manifest_path)
        duplicate_count += duplicates
    return manifest_paths, duplicate_count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="iolite Reference materials directory")
    parser.add_argument(
        "output",
        type=Path,
        help="UPbRPy reference_materials directory",
    )
    args = parser.parse_args()
    paths, duplicates = convert_directory(args.input, args.output)
    print(
        f"Converted {len(paths)} files; skipped {duplicates} "
        "exact duplicate result(s)."
    )
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
