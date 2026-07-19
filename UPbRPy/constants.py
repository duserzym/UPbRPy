"""Physical constants and packaged reference-material data."""

from importlib.resources import files

import pandas as pd

# Uranium isotope ratios
common_U238_U235 = 137.818
old_common_U238_U235 = 137.88

# Decay constants (year^-1) and their one-sigma uncertainties
lambda_235 = 9.8485e-10
sigma_235 = lambda_235 * 0.14 / 100 / 2
lambda_238 = 1.55125e-10
sigma_238 = lambda_238 * 0.11 / 100 / 2

standard_archive = pd.read_csv(files(__package__) / "standards.csv", index_col=0)

__all__ = [
    "common_U238_U235",
    "old_common_U238_U235",
    "lambda_235",
    "sigma_235",
    "lambda_238",
    "sigma_238",
    "standard_archive",
]
