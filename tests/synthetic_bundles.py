"""Synthetic Qtegra exports and session bundles shared by the tests."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import io
import json
from pathlib import Path
import zipfile


CHANNELS = ("29Si", "44Ca", "206Pb", "238U")
LABELS = ("NIST612_1", "MAD_UCSB_2", "AFB_3", "KD27_4")


def qtegra_csv(label: str, started: datetime, *, sweeps: int = 40) -> str:
    """Return a minimal Qtegra per-spot export the reader accepts."""
    header = f"{label}:{started.strftime('%m/%d/%Y %I:%M:%S %p')};\n"
    lines = [header] + ["Meta:x=1;\n"] * 12
    lines.append("Time," + ",".join(CHANNELS) + ",\n")
    lines.append("," + ",".join("dwell time=0.01" for _ in CHANNELS) + ",\n")
    for index in range(sweeps):
        signal = 10.0 if index < 12 else 1000.0 + index
        values = ",".join(f"{signal * (k + 1):.3f}" for k in range(len(CHANNELS)))
        lines.append(f"{index * 0.5:.3f},{values},\n")
    return "".join(lines)


def write_raw_directory(raw: Path, labels=LABELS) -> list[Path]:
    """Write one CSV per label, one minute apart, and return the paths."""
    raw.mkdir(parents=True, exist_ok=True)
    start = datetime(2025, 3, 20, 21, 0, 0)
    paths = []
    for index, label in enumerate(labels):
        path = raw / f"Synthetic_{index + 1}.csv"
        path.write_text(
            qtegra_csv(label, start + timedelta(minutes=index)),
            encoding="utf-8",
        )
        paths.append(path)
    return paths


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest_toml(
    *,
    session_id: str,
    session_key: str,
    raw_paths: list[Path],
    labels=LABELS,
    sep: str = "_",
    aliases: dict[str, str] | None = None,
) -> str:
    """Return a session_manifest.toml in the WORKFLOW.md 3.2 layout."""
    aliases = {"MAD": "MADUCSB", "AFB": "AFB1"} if aliases is None else aliases
    out = [
        "[session]",
        f'id = "{session_id}"',
        f'key = "{session_key}"',
        f"sep = {json.dumps(sep)}",
        'material = "apatite"',
        'instrument = "iCAP RQ"',
        'method = "Apatite U-Pb"',
        'operator = "Synthetic Operator"',
        "",
        "[samples]",
    ]
    out += [f'{alias} = "{target}"' for alias, target in aliases.items()]
    out += [
        "",
        "[reference_materials]",
        'primary_68 = "MADUCSB"',
        'concentration = ["NIST612"]',
        'secondary = ["AFB1"]',
        "",
    ]
    for index, (label, path) in enumerate(zip(labels, raw_paths)):
        kind = "sample" if label.startswith("KD") else "primary_standard"
        out += [
            "[[acquisition]]",
            f'label = "{label}"',
            f'acquisition_id = "acq-{index}"',
            f'spot_id = "spot-{index}"',
            f'grain_id = "grain-{index}"',
            f'sample_id = "sample-{index}"',
            f"started_at = 2025-03-20T21:0{index}:00",
            f'sha256 = "{sha256(path)}"',
            f'kind = "{kind}"',
            f'filename = "{path.name}"',
            "",
        ]
    return "\n".join(out)


def write_session_bundle(
    root: Path,
    *,
    session_id: str = "0192f0c4-0000-7000-8000-000000000001",
    session_key: str = "Synthetic_20Mar25",
    server: str = "http://127.0.0.1:1",
    upbr: bool = True,
) -> Path:
    """Write a session bundle folder ``root/<session_key>/`` and return it."""
    bundle = root / session_key
    raw_paths = write_raw_directory(bundle / "raw")
    (bundle / "session_manifest.toml").write_text(
        manifest_toml(
            session_id=session_id,
            session_key=session_key,
            raw_paths=raw_paths,
        ),
        encoding="utf-8",
    )
    (bundle / "README.md").write_text("Synthetic session bundle.\n")
    if upbr:
        (bundle / f"{session_key}.upbr").write_text(
            json.dumps(
                {
                    "format": "UPbRPy reduction project",
                    "format_version": 2,
                    "saved_utc": "2026-11-03T00:00:00+00:00",
                    "paths": {
                        "source_directory": {"relative": "raw", "absolute": ""},
                        "reference_library": None,
                    },
                    "pipeline": {
                        "config": {
                            "workflow_key": "apatite",
                            "material": "apatite",
                            "primary_channel": "44Ca",
                            "primary_68_standard": "MADUCSB",
                        },
                        "completed_stages": ["import", "selection"],
                        "resume_stage": "selection",
                    },
                    "selections": [],
                    "workspace": {},
                    "origin": {
                        "server": server,
                        "session_id": session_id,
                        "session_key": session_key,
                        "export_sha256": "",
                        "exported_at": "2026-11-03T12:00:00Z",
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return bundle


def zip_directory(directory: Path) -> bytes:
    """Zip ``directory`` with its own name as the single top-level folder."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.write(path, Path(directory.name) / path.relative_to(directory))
    return buffer.getvalue()
