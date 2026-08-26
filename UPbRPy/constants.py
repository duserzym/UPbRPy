"""Physical constants and packaged reference-material data."""

from importlib.resources import files

import pandas as pd

# Uranium isotope ratios
common_U238_U235 = 137.818
sigma_common_U238_U235 = 0.045 / 2
old_common_U238_U235 = 137.88

# Decay constants (year^-1) and their one-sigma uncertainties
lambda_235 = 9.8485e-10
sigma_235 = lambda_235 * 0.14 / 100 / 2
lambda_238 = 1.55125e-10
sigma_238 = lambda_238 * 0.11 / 100 / 2
# 232Th decay constant (year^-1), matching the value used by the legacy
# common-Pb correction routines. The direct spreadsheet Th-Pb date formula
# carries two additional insignificant digits (4.94752e-11).
lambda_232 = 4.9475e-11
# Relative two-sigma uncertainty used when thorium decay-constant uncertainty
# is explicitly enabled in an age calculation.
sigma_232 = 0.042e-11 / 2

# Retain the historical CSV archive as an explicit compatibility option. The
# primary ``standard_archive`` is constructed from the TOML library in
# :mod:`UPbRPy.standards`.
standard_archive_legacy = pd.read_csv(
    files(__package__) / "standards.csv",
    index_col=0,
)
standard_archive_csv = standard_archive_legacy

__all__ = [
    "common_U238_U235",
    "sigma_common_U238_U235",
    "old_common_U238_U235",
    "lambda_235",
    "sigma_235",
    "lambda_238",
    "sigma_238",
    "lambda_232",
    "sigma_232",
    "standard_archive_csv",
    "standard_archive_legacy",
]
