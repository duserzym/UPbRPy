"""Snapshot recalculated .upbr results and compare them without tolerances.

Usage: python scripts/snapshot_restore.py PROJECT.upbr OUT.pkl
       python scripts/snapshot_restore.py --compare A.pkl B.pkl

Only load pickle snapshots from trusted sources. Keep real-data snapshots
outside the repository. Callable names describe code, not its implementation;
the stored numerical products are what establish reduction identity.
"""

from __future__ import annotations

import argparse
from dataclasses import fields, is_dataclass
from pathlib import Path
import pickle
import sys

import numpy as np
import pandas as pd


def _qualified_name(value):
    cls = type(value)
    return f"{getattr(value, '__module__', cls.__module__)}.{getattr(value, '__qualname__', cls.__qualname__)}"


def _convert(value):
    """Keep numerical containers intact and remove local Python object types."""
    if isinstance(value, (pd.DataFrame, pd.Series, np.ndarray)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {_convert(key): _convert(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_convert(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_convert(item) for item in value)
    # Callable dataclasses (notably CalibrationModel) still carry fitted
    # coefficients and diagnostics; only their predictor becomes a name.
    if is_dataclass(value) and not isinstance(value, type):
        attributes = dict(vars(value)) if hasattr(value, "__dict__") else {}
        attributes.update({field.name: getattr(value, field.name) for field in fields(value)})
    elif callable(value):
        return {"__callable__": _qualified_name(value)}
    elif isinstance(value, (str, bytes, int, float, complex, bool, np.generic, type(None))):
        return value
    elif hasattr(value, "__dict__"):
        attributes = vars(value)
    else:
        return value
    return {
        "__type__": _qualified_name(type(value)),
        **{name: _convert(item) for name, item in attributes.items() if not name.startswith("__")},
    }


def snapshot_restored(restored) -> dict:
    """Extract canonical numerical products and workspace from RestoredProject."""
    session = restored.session
    values = {
        "reduction_cache": session.reduction_cache,
        "total_data": session.total_data,
        "channels": session.channels,
    }
    # Includes experiment and any additional Qtegra-derived session tables,
    # without copying the raw Qtegra acquisition arrays a second time.
    values.update({name: value for name, value in vars(session).items()
                   if not name.startswith("__") and isinstance(value, pd.DataFrame)})
    qtegra = getattr(session, "qtegra", None)
    if hasattr(qtegra, "__dict__"):
        tables = {name: value for name, value in vars(qtegra).items()
                  if not name.startswith("__") and isinstance(value, pd.DataFrame)}
        if tables:
            values["qtegra_tables"] = tables
    values.update(
        completed_stages=restored.completed_stages,
        resume_stage=restored.resume_stage,
        workspace=restored.workspace,
    )
    return _convert(values)


def snapshot_project(project: str | Path) -> dict:
    """Restore a saved project and snapshot every recalculated GUI cache entry."""
    # Direct execution puts scripts/, rather than the checkout root, on sys.path.
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from GUI.services import restore_session_from_project

    return snapshot_restored(restore_session_from_project(project))


def compare_snapshots(a, b) -> list[str]:
    """Return up to 50 exact differences, with paths and insertion order checked."""
    differences = []

    def report(path, message):
        if len(differences) < 50:
            differences.append(f"{path}: {message}")

    def compare(left, right, path):
        if len(differences) >= 50:
            return
        if type(left) is not type(right):
            report(path, f"types differ ({type(left).__name__} != {type(right).__name__})")
            return
        if isinstance(left, (pd.DataFrame, pd.Series)):
            assertion = (pd.testing.assert_frame_equal if isinstance(left, pd.DataFrame)
                         else pd.testing.assert_series_equal)
            try:
                assertion(left, right, check_exact=True, check_like=False)
            except AssertionError as exc:
                report(path, str(exc))
            compare(left.attrs, right.attrs, f"{path}.attrs")
        elif isinstance(left, np.ndarray):
            if left.dtype != right.dtype:
                report(f"{path}.dtype", f"{left.dtype} != {right.dtype}")
            if left.shape != right.shape:
                report(f"{path}.shape", f"{left.shape} != {right.shape}")
                return
            try:
                equal = np.array_equal(left, right, equal_nan=True)
            except TypeError:
                # Strings and mixed object arrays cannot always use isnan.
                # Recursion also handles arrays stored inside object arrays.
                if left.dtype.hasobject:
                    for index in np.ndindex(left.shape):
                        compare(left[index], right[index], f"{path}{index}")
                    return
                equal = np.array_equal(left, right)
            if not equal:
                report(path, "array values differ")
        elif isinstance(left, dict):
            if list(left) != list(right):
                report(path, f"keys/order differ ({list(left)!r} != {list(right)!r})")
            for key in left:
                if key in right:
                    compare(left[key], right[key], f"{path}[{key!r}]")
                if len(differences) >= 50:
                    break
        elif isinstance(left, (list, tuple)):
            if len(left) != len(right):
                report(path, f"lengths differ ({len(left)} != {len(right)})")
            for index, (item_a, item_b) in enumerate(zip(left, right)):
                compare(item_a, item_b, f"{path}[{index}]")
                if len(differences) >= 50:
                    break
        else:
            if isinstance(left, (float, complex, np.inexact)):
                if np.isnan(left) and np.isnan(right):
                    return
            if left is right:  # Also handles pandas.NA and pandas.NaT.
                return
            if left != right:
                report(path, f"{left!r} != {right!r}")

    compare(a, b, "snapshot")
    return differences


def main(argv=None) -> int:
    """Write a snapshot, or compare two trusted pickle files (exit 1 on drift)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("source", type=Path, help="PROJECT.upbr, or A.pkl with --compare")
    parser.add_argument("target", type=Path, help="OUT.pkl, or B.pkl with --compare")
    args = parser.parse_args(argv)
    if args.compare:
        with args.source.open("rb") as stream:
            baseline = pickle.load(stream)
        with args.target.open("rb") as stream:
            current = pickle.load(stream)
        differences = compare_snapshots(baseline, current)
        print("\n".join(differences) if differences else "Snapshots are identical.")
        return int(bool(differences))
    snapshot = snapshot_project(args.source)
    with args.target.open("wb") as stream:
        pickle.dump(snapshot, stream, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"Wrote {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
