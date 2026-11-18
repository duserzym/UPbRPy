"""Independent calibration calculations and legacy numerical compatibility."""

import math

import numpy as np
import pandas as pd
import pytest

from UPbRPy.export_table import ExportSettings
from UPbRPy.thermometry import (
    THERMOMETERS, Ti_in_zircon_thermometry, Zr_in_rutile_thermometry,
    Zr_in_titanite_thermometry, thermometer_for,
)


@pytest.mark.parametrize("concentration", [5., 50.])
def test_ferry_zircon_log_form(concentration):
    silica, titania = .8, .6
    kelvin = 4800 / (5.711 - math.log10(silica) + math.log10(titania) - math.log10(concentration))
    assert Ti_in_zircon_thermometry(concentration, silica, titania) == round(kelvin - 273.15, 2)
    assert math.log10(concentration) == pytest.approx(
        5.711 - 4800 / kelvin - math.log10(silica) + math.log10(titania))


@pytest.mark.parametrize("concentration", [100., 1000.])
def test_ferry_rutile_log_form(concentration):
    silica = .8
    kelvin = 4530 / (7.420 - math.log10(silica) - math.log10(concentration))
    assert Zr_in_rutile_thermometry(concentration, 2., silica, "FW2007") == round(kelvin - 273.15, 2)
    assert math.log10(concentration) == pytest.approx(7.420 - 4530 / kelvin - math.log10(silica))


@pytest.mark.parametrize("concentration", [100., 1000.])
def test_kohn_pressure_in_bar(concentration):
    pressure_gpa = 1.7
    kelvin = (71360 + .378 * pressure_gpa * 10000 - .130 * concentration) / (130.66 - 8.314 * math.log(concentration))
    assert Zr_in_rutile_thermometry(concentration, pressure_gpa) == round(kelvin - 273.15, 2)
    wrong_units = (71360 + .378 * pressure_gpa - .130 * concentration) / (130.66 - 8.314 * math.log(concentration)) - 273.15
    assert abs(Zr_in_rutile_thermometry(concentration, pressure_gpa) - wrong_units) > 10


@pytest.mark.parametrize("concentration", [100., 1000.])
def test_hayden_log_form_and_scalar_rounding(concentration):
    silica, titania, pressure = .8, .6, 1.7
    kelvin = (7708 + 960 * pressure) / (10.52 - math.log10(concentration) - math.log10(titania) - math.log10(silica))
    assert Zr_in_titanite_thermometry(concentration, silica, titania, pressure) == round(kelvin - 273.15, 2)
    assert math.log10(concentration) == pytest.approx(
        10.52 - 7708 / kelvin - 960 * pressure / kelvin - math.log10(titania) - math.log10(silica))


CALIBRATIONS = [(material, calibration) for material, thermometer in THERMOMETERS.items()
                for calibration in thermometer.calibrations]


@pytest.mark.parametrize("material,calibration", CALIBRATIONS, ids=[c.key for _, c in CALIBRATIONS])
def test_registry_matches_legacy_functions_and_preserves_series(material, calibration):
    settings = ExportSettings(a_sio2=.8, a_tio2=.7, pressure_gpa=1.7)
    concentration = pd.Series([10., 100.], index=["spot-b", "spot-a"], name="ppm")
    if material == "zircon":
        expected = Ti_in_zircon_thermometry(concentration, .8, .7)
    elif material == "rutile":
        expected = Zr_in_rutile_thermometry(concentration, 1.7, .8, calibration.key)
    else:
        expected = Zr_in_titanite_thermometry(concentration, .8, .7, 1.7)
    pd.testing.assert_series_equal(calibration.temperature(concentration, settings), expected)
    np.testing.assert_array_equal(calibration.temperature(concentration.to_numpy(), settings), expected.to_numpy())
    assert calibration.temperature(10., settings) == expected.iloc[0]


@pytest.mark.parametrize("material,calibration", CALIBRATIONS, ids=[c.key for _, c in CALIBRATIONS])
def test_invalid_concentrations_are_nan(material, calibration):
    settings = ExportSettings()
    assert np.isnan(calibration.temperature([0, -1, np.nan, None], settings)).all()
    values = pd.Series([0, -1, pd.NA, None], dtype="Float64")
    assert calibration.temperature(values, settings).isna().all()


def test_material_lookup_and_calibration_selection():
    assert thermometer_for("zircon") is THERMOMETERS["zircon"]
    assert thermometer_for("apatite") is None
    assert thermometer_for(None) is None
    rutile = thermometer_for("rutile")
    assert rutile.calibration(ExportSettings()).key == "Kohn2020"
    assert rutile.calibration(ExportSettings(rutile_calibration="FW2007")).key == "FW2007"
