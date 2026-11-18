"""Citation formatting and coverage of the export catalogue."""

from dataclasses import replace
import re

import pytest

from UPbRPy.export_citations import GROUPS, REFERENCES, GroupInfo, group_info, reference
from UPbRPy.export_table import _ELEMENT_MASSES, _catalogue
from UPbRPy.thermometry import THERMOMETERS


@pytest.mark.parametrize("key", REFERENCES)
def test_reference_style_and_doi(key):
    ref = reference(key)
    assert ref.key == key
    if ref.verified and ref.doi:
        assert re.fullmatch(r"10\.\d{4,9}/\S+", ref.doi)
    authors = re.findall(r"(?:^|, )([^,]+), [^,]+", ref.authors)
    assert authors
    name = authors[0] if len(authors) == 1 else (
        f"{authors[0]} and {authors[1]}" if len(authors) == 2 else f"{authors[0]} et al.")
    assert ref.short == f"{name} ({ref.year})"
    assert ref.footnote() == f"{ref.short}, {ref.journal_abbrev} {ref.volume}, {ref.pages}, doi:{ref.doi}"
    assert ref.doi in ref.full()
    assert ref.journal in ref.full()
    assert ref.title in ref.full()
    assert ref.authors in ref.full()


def test_unverified_note_and_absent_doi():
    ref = replace(reference("ferry2007"), verified=False, note="Not verified", doi="")
    assert ref.footnote().endswith("; Not verified")
    assert "doi:" not in ref.full() + ref.footnote()
    assert "Published online" not in reference("hayden2008").footnote()


def test_every_catalogue_group_and_calibration_has_registry_info():
    columns = _catalogue(sorted(_ELEMENT_MASSES, key=_ELEMENT_MASSES.get))
    assert {column.group for column in columns} == set(GROUPS)
    for name, info in GROUPS.items():
        assert group_info(name) is info
        assert info.group == name
        assert info.section in {"core", "trace", "literature", "thermometry"}
        assert set(info.references) <= REFERENCES.keys()
        if info.section == "core":
            assert info.title == name and info.references == ()
    for thermometer in THERMOMETERS.values():
        assert GROUPS[thermometer.group].section == "thermometry"
        assert {c.reference for c in thermometer.calibrations} <= set(GROUPS[thermometer.group].references)
    assert group_info("Future group") == GroupInfo("Future group", "core", "Future group", (), "", "")
    with pytest.raises(KeyError):
        reference("unknown")
