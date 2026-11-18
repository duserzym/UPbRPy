"""Mineral thermometry calculations and uncertainty envelopes.

Ferry and Watson (2007): doi:10.1007/s00410-007-0201-0.
Kohn (2020): doi:10.2138/am-2020-7091.
Hayden, Watson and Wark (2008): doi:10.1007/s00410-007-0256-y.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

def Zr_in_rutile_thermometry(Zr_ppm, pressure, a_SiO2=1, method='Kohn2020'):
    '''
    function for calculating Zr in rutile thermometer after Kohn (2020) or Ferry and Watson (2007)

    Parameters
    ----------
    Zr_ppm : float
        Zr concentration in rutile in ppm
    pressure : float
        pressure in GPa
    a_SiO2 : float
        activity of SiO2
    method : str
        method for calculation, either 'Kohn2020' or 'FW2007'

    Returns
    -------
    float
        temperature in degree Celsius
    '''
    R = 8.314
    if method == 'Kohn2020':
        pressure = pressure * 1e4 # convert to bar
        T_K = (71360 + 0.378 * pressure - 0.130 * Zr_ppm) / (130.66 - R*np.log(Zr_ppm)) 
    elif method == 'FW2007':
        T_K = 4530 / (7.420 - np.log10(a_SiO2) - np.log10(Zr_ppm))
    return np.round(T_K - 273.15, 2)

def Zr_in_rutile_thermometry_uncertainty(Zr_ppm, pressure_list, a_SiO2_list, method='Kohn2020'):
    '''
    function for calculating Zr in rutile thermometer uncertainty after Kohn (2020) or Ferry and Watson (2007)

    Parameters
    ----------
    Zr_ppm : float
        Zr concentration in rutile in ppm
    pressure_list : list
        list of pressure in GPa
    a_SiO2_list : list
        list of activity of SiO2
    method : str
        method for calculation, either 'Kohn2020' or 'FW2007'

    Returns
    -------
    upper : float
        the 97.5 percentile temperature in degree Celsius
    lower : float
        the 2.5 percentile temperature in degree Celsius
    '''
    T_list = []
    for pressure, a_SiO2 in zip(pressure_list, a_SiO2_list):
        T_list.append(Zr_in_rutile_thermometry(Zr_ppm, pressure, a_SiO2, method))
    upper, lower = np.percentile(T_list, [97.5, 2.5])
    return upper, lower

def Ti_in_zircon_thermometry(Ti_ppm, a_SiO2, a_TiO2):
    '''
    function for calculating Ti in zircon thermometer after Ferry and Watson (2007)
    
    Parameters
    ----------
    Ti_ppm : float
        Ti concentration in zircon in ppm
    a_SiO2 : float
        activity of SiO2
    a_TiO2 : float
        activity of TiO2

    Returns
    -------
    float
        temperature in degree Celsius

    '''

    T_K = 4800 / (5.711 - np.log10(a_SiO2) + np.log10(a_TiO2) - np.log10(Ti_ppm))

    return np.round(T_K - 273.15, 2)

def Ti_in_zircon_thermometry_uncertainty(Ti_ppm, a_SiO2_list, a_TiO2_list):
    '''
    function for calculating Ti in zircon thermometer uncertainty
    
    Parameters
    ----------
    Ti_ppm : float
        Ti concentration in zircon in ppm
    a_SiO2_list : list
        list of activity of SiO2
    a_TiO2_list : list
        list of activity of TiO2

    Returns
    -------
    float
        temperature in degree Celsius

    '''

    T_list = []
    for a_SiO2, a_TiO2 in zip(a_SiO2_list, a_TiO2_list):
        T_list.append(Ti_in_zircon_thermometry(Ti_ppm, a_SiO2, a_TiO2))
    upper, lower = np.percentile(T_list, [97.5, 2.5])
    return upper, lower

def Zr_in_titanite_thermometry(Zr_ppm, a_SiO2, a_TiO2, pressure):
    '''
    function for calculating Zr in titanite thermometer after Hayden, Watson and Wark (2008)
    
    Parameters
    ----------
    Zr_ppm : float
        Zr concentration in titanite in ppm
    a_SiO2 : float
        activity of SiO2
    a_TiO2 : float
        activity of TiO2
    pressure : float
        pressure in GPa

    Returns
    -------
    float
        temperature in degree Celsius

    '''

    T_K = (7708 + 960 * pressure) / (10.52 - np.log10(Zr_ppm) - np.log10(a_TiO2) - np.log10(a_SiO2))

    return np.round(T_K - 273.15, 2)

def Zr_in_titanite_thermometry_uncertainty(Zr_ppm, a_SiO2_list, a_TiO2_list, pressure_list):
    '''
    function for calculating Zr in titanite thermometer uncertainty 
    
    Parameters
    ----------
    Zr_ppm : float
        Zr concentration in titanite in ppm
    a_SiO2_list : list
        list of activity of SiO2
    a_TiO2_list : list
        list of activity of TiO2
    pressure : float
        pressure in GPa

    Returns
    -------
    float
        temperature in degree Celsius

    '''

    T_list = []
    for a_SiO2, a_TiO2, pressure in zip(a_SiO2_list, a_TiO2_list, pressure_list):
        T_list.append(Zr_in_titanite_thermometry(Zr_ppm, a_SiO2, a_TiO2, pressure))
    upper, lower = np.percentile(T_list, [97.5, 2.5])
    return upper, lower

__all__ = [
    'Zr_in_rutile_thermometry',
    'Zr_in_rutile_thermometry_uncertainty',
    'Ti_in_zircon_thermometry',
    'Ti_in_zircon_thermometry_uncertainty',
    'Zr_in_titanite_thermometry',
    'Zr_in_titanite_thermometry_uncertainty',
]


@dataclass(frozen=True)
class ThermometerInput:
    key: str
    label: str
    minimum: float
    maximum: float
    step: float
    decimals: int
    hint: str


@dataclass(frozen=True)
class Calibration:
    key: str
    reference: str
    formula_text: str
    inputs: tuple[ThermometerInput, ...]
    fixed: tuple[str, ...]

    def temperature(self, element_ppm, settings) -> np.ndarray | pd.Series:
        """Evaluate the legacy calibration, masking missing or nonpositive ppm."""
        if isinstance(element_ppm, pd.Series):
            concentration = pd.to_numeric(element_ppm, errors="coerce").astype(float)
            concentration = concentration.where(concentration > 0)
        else:
            concentration = np.asarray(element_ppm, dtype=float)
            concentration = np.where(concentration > 0, concentration, np.nan)
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            if self.key == "FW2007_zircon":
                return Ti_in_zircon_thermometry(concentration, settings.a_sio2, settings.a_tio2)
            if self.key in {"Kohn2020", "FW2007"}:
                return Zr_in_rutile_thermometry(concentration, settings.pressure_gpa, settings.a_sio2, self.key)
            if self.key == "Hayden2008":
                return Zr_in_titanite_thermometry(concentration, settings.a_sio2, settings.a_tio2, settings.pressure_gpa)
        raise ValueError(f"Unknown calibration: {self.key}")


@dataclass(frozen=True)
class Thermometer:
    key: str
    material: str
    element: str
    column: str
    group: str
    title: str
    calibrations: tuple[Calibration, ...]
    settings_field: str | None

    def calibration(self, settings) -> Calibration:
        """Select the configured calibration, or the first one by default."""
        if self.settings_field is None:
            return self.calibrations[0]
        key = getattr(settings, self.settings_field)
        for calibration in self.calibrations:
            if calibration.key == key:
                return calibration
        raise ValueError(f"Unknown {self.material} calibration: {key}")


_SILICA = ThermometerInput("a_sio2", "aSiO₂", .001, 1., .05, 3, "1 with quartz")
_TITANIA_ZIRCON = ThermometerInput("a_tio2", "aTiO₂", .001, 1., .05, 3,
    "1 with rutile; often 0.5–0.7 in rutile-free magmas")
_TITANIA_TITANITE = ThermometerInput("a_tio2", "aTiO₂", .001, 1., .05, 3,
    "often 0.5–0.9 without rutile")
_PRESSURE = ThermometerInput("pressure_gpa", "P (GPa)", 0., 10., .1, 2,
    "pressure of crystallization")

THERMOMETERS: dict[str, Thermometer] = {
    "zircon": Thermometer("ti_in_zircon", "zircon", "Ti", "Ti_in_zircon_C", "Ti-in-zircon",
        "Ti-in-zircon temperature", (
            Calibration("FW2007_zircon", "ferry2007",
                "log10(Ti) = 5.711 − 4800/T − log10(aSiO2) + log10(aTiO2); T in K, Ti in µg/g",
                (_SILICA, _TITANIA_ZIRCON), ()),
        ), None),
    "rutile": Thermometer("zr_in_rutile", "rutile", "Zr", "Zr_in_rutile_C", "Zr-in-rutile",
        "Zr-in-rutile temperature", (
            Calibration("Kohn2020", "kohn2020",
                "T = (71360 + 0.378·P − 0.130·C) / (130.66 − R·ln C); T in K, C = Zr in µg/g, P in bar (input in GPa, ×10⁴), R = 8.314 J/(mol·K)",
                (_PRESSURE,), ("Assumes rutile coexists with quartz and zircon (aSiO2 = aZrSiO4 = 1)",)),
            Calibration("FW2007", "ferry2007",
                "log10(Zr) = 7.420 − 4530/T − log10(aSiO2); T in K, Zr in µg/g",
                (_SILICA,), ("No pressure term; assumes zircon is present",)),
        ), "rutile_calibration"),
    "titanite": Thermometer("zr_in_titanite", "titanite", "Zr", "Zr_in_titanite_C", "Zr-in-titanite",
        "Zr-in-titanite temperature", (
            Calibration("Hayden2008", "hayden2008",
                "log10(Zr) = 10.52 − 7708/T − 960·P/T − log10(aTiO2) − log10(aSiO2); T in K, Zr in µg/g, P in GPa",
                (_SILICA, _TITANIA_TITANITE, _PRESSURE), ()),
        ), None),
}


def thermometer_for(material) -> Thermometer | None:
    """Return a material's thermometer, or None if no calibration is registered."""
    return THERMOMETERS.get(material)


__all__ += ["ThermometerInput", "Calibration", "Thermometer", "THERMOMETERS", "thermometer_for"]
