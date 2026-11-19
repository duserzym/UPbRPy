"""Natural ordering of instrument and sample labels."""

import pytest

from UPbRPy import natural_sort_key
from UPbRPy.naming import natural_sort_key as focused_key


@pytest.mark.parametrize("expected", [
    [f"LV25-2_S_{n}" for n in (1, 2, 10, 11, 100)],
    [f"KH2-30.7 m {n}" for n in (2, 12, 100)],
    ["CI2-dz1_9", "CI2-dz1_10"],
    ["91500-3", "91500-12"],
    ["alpha_2", "Alpha_10", "BETA_1"],
    ["_07", "_7", "_10"],
    ["", "2", "10", "A", "a"],
    ["core 2.7_2", "core 2.7_10", "core 2.12_1"],
    ["2a", "a", "a2", "aa"],
])
def test_natural_label_order(expected):
    assert sorted(reversed(expected), key=natural_sort_key) == expected


def test_casefold_and_original_string_tiebreak():
    assert natural_sort_key is focused_key
    assert natural_sort_key("Straße_2")[0] == natural_sort_key("STRASSE_02")[0]
    assert sorted(["a_7", "A_07", "a_07", "A_7"], key=natural_sort_key) == [
        "A_07", "A_7", "a_07", "a_7",
    ]
