"""Compare arithmetic and log-ratio spot summaries on exported sweep data.

The benchmark consumes existing UPbRPy depth-profile audit tables.  It does
not refit fractionation models; instead it isolates the estimator sensitivity
of already corrected positive sweep ratios.  This makes the result fast,
reproducible, and independent of notebook rendering.

Example
-------
python benchmarks/benchmark_ratio_aggregation.py \
    --input zircon=path/to/zircon_depthpro_data_table.csv \
    --input apatite=path/to/apatite_depthpro_data_table.csv \
    --output ratio_aggregation_sensitivity.csv
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from UPbRPy.ratios import aggregate_ratio


DEFAULT_RATIOS = (
    "206Pb/238U_fractionation_corrected",
    "207Pb/206Pb_fractionation_corrected",
    "208Pb/232Th_fractionation_corrected",
)


def parse_labeled_path(value: str) -> tuple[str, Path]:
    """Parse a command-line ``label=path`` input specification."""
    if "=" not in value:
        raise argparse.ArgumentTypeError("inputs must use label=path syntax")
    label, path = value.split("=", 1)
    if not label.strip() or not path.strip():
        raise argparse.ArgumentTypeError("inputs must use label=path syntax")
    return label.strip(), Path(path).expanduser()


def summarize_file(label: str, path: Path, ratios=DEFAULT_RATIOS) -> pd.DataFrame:
    """Return one row per spot and corrected ratio in an audit table."""
    required = {"spot_name", *ratios}
    frame = pd.read_csv(path, usecols=lambda column: column in required)
    missing = required - set(frame)
    if missing:
        raise KeyError(
            f"{path} is missing benchmark columns: {', '.join(sorted(missing))}"
        )

    records = []
    for spot_name, spot in frame.groupby("spot_name", sort=False):
        for ratio in ratios:
            values = pd.to_numeric(spot[ratio], errors="coerce").to_numpy()
            values = values[np.isfinite(values) & (values > 0)]
            if values.size < 2:
                continue
            arithmetic = aggregate_ratio(values, "arithmetic")
            geometric = aggregate_ratio(values, "logratio")
            reciprocal_arithmetic = aggregate_ratio(1 / values, "arithmetic")
            reciprocal_geometric = aggregate_ratio(1 / values, "logratio")
            records.append(
                {
                    "material": label,
                    "source": str(path),
                    "spot_name": str(spot_name),
                    "ratio": ratio.replace("_fractionation_corrected", ""),
                    "n_sweeps": int(values.size),
                    "arithmetic_mean": arithmetic,
                    "geometric_mean": geometric,
                    "geometric_minus_arithmetic_percent": (
                        100 * (geometric / arithmetic - 1)
                    ),
                    "arithmetic_reciprocal_closure_percent": (
                        100 * (arithmetic * reciprocal_arithmetic - 1)
                    ),
                    "geometric_reciprocal_closure_percent": (
                        100 * (geometric * reciprocal_geometric - 1)
                    ),
                }
            )
    return pd.DataFrame.from_records(records)


def summarize_sensitivity(detail: pd.DataFrame) -> pd.DataFrame:
    """Summarize estimator differences by material and isotope ratio."""
    difference = "geometric_minus_arithmetic_percent"
    closure = "arithmetic_reciprocal_closure_percent"
    rows = []
    for (material, ratio), group in detail.groupby(["material", "ratio"]):
        absolute_difference = group[difference].abs()
        rows.append(
            {
                "material": material,
                "ratio": ratio,
                "spot_count": int(len(group)),
                "median_difference_percent": float(group[difference].median()),
                "median_absolute_difference_percent": float(
                    absolute_difference.median()
                ),
                "p95_absolute_difference_percent": float(
                    absolute_difference.quantile(0.95)
                ),
                "maximum_absolute_difference_percent": float(
                    absolute_difference.max()
                ),
                "median_arithmetic_reciprocal_nonclosure_percent": float(
                    group[closure].median()
                ),
                "maximum_geometric_reciprocal_nonclosure_percent": float(
                    group["geometric_reciprocal_closure_percent"].abs().max()
                ),
            }
        )
    return pd.DataFrame.from_records(rows)


def main(argv=None) -> int:
    """Run the command-line benchmark and write detail plus summary tables."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        action="append",
        type=parse_labeled_path,
        required=True,
        metavar="LABEL=CSV",
        help="Labeled UPbRPy depth-profile audit table; repeat as needed.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Summary CSV path. A .detail.csv companion is also written.",
    )
    args = parser.parse_args(argv)

    detail = pd.concat(
        [summarize_file(label, path) for label, path in args.input],
        ignore_index=True,
    )
    summary = summarize_sensitivity(detail)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    detail_path = args.output.with_suffix(".detail.csv")
    summary.to_csv(args.output, index=False)
    detail.to_csv(detail_path, index=False)
    print(summary.to_string(index=False))
    print(f"\nWrote {args.output} and {detail_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
