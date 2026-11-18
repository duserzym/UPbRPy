"""Total alpha events from present-day U and Th; no annealing is modelled."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .constants import common_U238_U235, lambda_232, lambda_235, lambda_238

AVOGADRO_CONSTANT = 6.02214076e23
M_U = 238.02891
M_TH = 232.0377

ALPHA_DOSE_TOOLTIP = (
    "D = 8 N238 (exp(λ238 t) − 1) + 7 N235 (exp(λ235 t) − 1) "
    "+ 6 N232 (exp(λ232 t) − 1), α/g.\n"
    f"N_A = {AVOGADRO_CONSTANT:.8e} atoms/mol; M_U = {M_U} and M_Th = {M_TH} g/mol; "
    f"238U/235U = {common_U238_U235}.\n"
    f"λ238 = {lambda_238:g}, λ235 = {lambda_235:g}, λ232 = {lambda_232:g} per year; t in years.\n"
    "t = accepted date of each reference material (accepted 206Pb/238U date from the library); "
    "t = the spot's preferred date for unknowns.\n"
    "Assumes no annealing, so it is an upper bound on accumulated radiation damage; "
    "present-day U and Th from the session's µg/g calibration."
)


def alpha_dose(u_ug_g, th_ug_g, age_ma, *, u238_u235=common_U238_U235) -> np.ndarray | float | pd.Series:
    """Return total alpha events per gram, broadcasting concentrations and Ma ages.

    Negative or nonfinite inputs give NaN. Scalar inputs return a float; a
    one-dimensional Series input retains its index and returns a Series.
    U isotope fractions are atom fractions of the present-day uranium.
    """
    series = next((v for v in (u_ug_g, th_ug_g, age_ma) if isinstance(v, pd.Series)), None)
    u, th, age, ratio = np.broadcast_arrays(*[
        np.asarray(v, dtype=float) for v in (u_ug_g, th_ug_g, age_ma, u238_u235)])
    valid = (np.isfinite(u) & (u >= 0) & np.isfinite(th) & (th >= 0)
             & np.isfinite(age) & (age >= 0) & np.isfinite(ratio) & (ratio > 0))
    n_u = u * 1e-6 * AVOGADRO_CONSTANT / M_U
    n238, n235 = n_u * ratio / (1 + ratio), n_u / (1 + ratio)
    n232 = th * 1e-6 * AVOGADRO_CONSTANT / M_TH
    years = age * 1e6
    with np.errstate(over="ignore", invalid="ignore"):
        dose = (8 * n238 * np.expm1(lambda_238 * years)
                + 7 * n235 * np.expm1(lambda_235 * years)
                + 6 * n232 * np.expm1(lambda_232 * years))
    dose = np.where(valid, dose, np.nan)
    if dose.ndim == 0:
        return float(dose)
    if series is not None and dose.ndim == 1:
        return pd.Series(dose, index=series.index, name=series.name)
    return dose


__all__ = ["alpha_dose", "ALPHA_DOSE_TOOLTIP", "AVOGADRO_CONSTANT", "M_U", "M_TH"]
