"""References and purpose notes for publication-table column groups."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Reference:
    key: str
    short: str
    authors: str
    year: int
    title: str
    journal: str
    volume: str
    pages: str
    doi: str
    journal_abbrev: str
    verified: bool = True
    note: str = ""

    def full(self) -> str:
        """Return the full bibliographic citation."""
        text = f"{self.authors} ({self.year}). {self.title}. {self.journal} {self.volume}, {self.pages}."
        return text + (f" doi:{self.doi}" if self.doi else "")

    def footnote(self) -> str:
        """Return the abbreviated citation, flagging unverified records."""
        text = f"{self.short}, {self.journal_abbrev} {self.volume}, {self.pages}"
        if self.doi:
            text += f", doi:{self.doi}"
        if not self.verified and self.note:
            text += f"; {self.note}"
        return text


REFERENCES: dict[str, Reference] = {
    ref.key: ref for ref in (
        Reference("grimes2015", "Grimes et al. (2015)",
            "Grimes, C.B., Wooden, J.L., Cheadle, M.J., John, B.E.", 2015,
            '\"Fingerprinting\" tectono-magmatic provenance using trace elements in igneous zircon',
            "Contributions to Mineralogy and Petrology", "170", "46",
            "10.1007/s00410-015-1199-3", "Contrib. Mineral. Petrol."),
        Reference("itano2024", "Itano and Sawada (2024)", "Itano, K., Sawada, H.", 2024,
            "Revisiting the geochemical classification of zircon source rocks using a machine learning approach",
            "Mathematical Geosciences", "56", "1139–1160", "10.1007/s11004-023-10128-z", "Math. Geosci.",
            note='Labelled "Itano \'24" in UPbR26plus; its Fig. 10 plots P vs REE+Y and Nb/P vs Ce/P.'),
        Reference("sawaki2022", "Sawaki et al. (2022)",
            "Sawaki, Y., Asanuma, H., Sakata, S., Abe, M., Ohno, T.", 2022,
            "Zircon trace-element compositions in Miocene granitoids in Japan: discrimination diagrams for zircons in M-, I-, S-, and A-type granites",
            "Island Arc", "31", "e12466", "10.1111/iar.12466", "Island Arc"),
        Reference("burnham2017", "Burnham and Berry (2017)", "Burnham, A.D., Berry, A.J.", 2017,
            "Formation of Hadean granites by melting of igneous crust", "Nature Geoscience", "10", "457–461",
            "10.1038/ngeo2942", "Nat. Geosci."),
        Reference("lu2016", "Lu et al. (2016)",
            "Lu, Y.J., Loucks, R.R., Fiorentini, M., McCuaig, T.C., Evans, N.J., Yang, Z.M., Hou, Z.Q., Kirkland, C.L., Parra-Avila, L.A., Kobussen, A.", 2016,
            "Zircon compositions as a pathfinder for porphyry Cu ± Mo ± Au deposits",
            "Tectonics and Metallogeny of the Tethyan Orogenic Belt, SEG Special Publication", "19", "329–347",
            "10.5382/SP.19.13", "SEG Spec. Publ."),
        Reference("pizarro2020", "Pizarro et al. (2020)",
            "Pizarro, H., Campos, E., Bouzari, F., Rousse, S., Bissig, T., Gregoire, M., Riquelme, R.", 2020,
            "Porphyry indicator zircons (PIZs): Application to exploration of porphyry copper deposits",
            "Ore Geology Reviews", "126", "103771", "10.1016/j.oregeorev.2020.103771", "Ore Geol. Rev."),
        Reference("roberts2024", "Roberts et al. (2024)",
            "Roberts, N.M.W., Yakymchuk, C., Spencer, C.J., Keller, C.B., Tapster, S.R.", 2024,
            "Revisiting the discrimination and distribution of S-type granites from zircon trace element composition",
            "Earth and Planetary Science Letters", "633", "118638", "10.1016/j.epsl.2024.118638", "Earth Planet. Sci. Lett."),
        Reference("loucks2024", "Loucks et al. (2024)",
            "Loucks, R.R., Henríquez, G.J., Fiorentini, M.L.", 2024,
            "Zircon and whole-rock trace element indicators of magmatic hydration state and oxidation state discriminate copper ore-forming from barren arc magmas",
            "Economic Geology", "119", "511–523", "10.5382/econgeo.5071", "Econ. Geol."),
        Reference("sun1989", "Sun and McDonough (1989)", "Sun, S.-s., McDonough, W.F.", 1989,
            "Chemical and isotopic systematics of oceanic basalts: implications for mantle composition and processes",
            "Geological Society, London, Special Publications", "42", "313–345",
            "10.1144/GSL.SP.1989.042.01.19", "Geol. Soc. Lond. Spec. Publ."),
        Reference("ferry2007", "Ferry and Watson (2007)", "Ferry, J.M., Watson, E.B.", 2007,
            "New thermodynamic models and revised calibrations for the Ti-in-zircon and Zr-in-rutile thermometers",
            "Contributions to Mineralogy and Petrology", "154", "429–437",
            "10.1007/s00410-007-0201-0", "Contrib. Mineral. Petrol."),
        Reference("kohn2020", "Kohn (2020)", "Kohn, M.J.", 2020,
            "A refined zirconium-in-rutile thermometer", "American Mineralogist", "105", "963–971",
            "10.2138/am-2020-7091", "Am. Mineral."),
        Reference("hayden2008", "Hayden et al. (2008)", "Hayden, L.A., Watson, E.B., Wark, D.A.", 2008,
            "A thermobarometer for sphene (titanite)", "Contributions to Mineralogy and Petrology", "155", "529–540",
            "10.1007/s00410-007-0256-y", "Contrib. Mineral. Petrol.",
            note="Published online 2007; volume year 2008."),
    )
}


@dataclass(frozen=True)
class GroupInfo:
    group: str
    section: str
    title: str
    references: tuple[str, ...]
    used_for: str
    caveat: str


GROUPS: dict[str, GroupInfo] = {
    name: GroupInfo(name, "core", name, (), "", "")
    for name in ("Analysis", "Composition", "Isotope ratios", "Dates", "Common-Pb corrected", "Preferred date")
}
GROUPS.update({info.group: info for info in (
    GroupInfo("Chondrite-normalized", "trace", "Chondrite-normalized", ("sun1989",),
        "REE divided by CI chondrite values, for REE patterns and anomalies.",
        "Normalizing values differ between compilations; compare only data normalized the same way."),
    GroupInfo("Trace-element ratios", "trace", "Trace-element ratios", ("sun1989",),
        "Common screening ratios: Ce and Eu anomalies, ΣREE, HREE slope, Nb/Ta, Lu/Hf.",
        "Ce/Ce* depends on La and Pr, which inclusions and low counts affect strongly."),
    GroupInfo("Concentrations", "trace", "Concentrations", (),
        "Calibrated trace-element mass fractions.", ""),
    GroupInfo("Grimes et al. (2015)", "literature", "Tectonic setting", ("grimes2015",),
        "U/Yb against Nb/Yb (the mantle-zircon array) and Ce/Yb, Gd/Yb separate zircon from continental arcs, ocean islands and mid-ocean ridges.",
        "Fields overlap and apply to igneous zircon; the paper's Sc/Yb discriminant is not exported."),
    GroupInfo("Itano and Sawada (2024)", "literature", "Granite type (I, S, A)", ("itano2024", "sawaki2022", "burnham2017"),
        "Low Nb/P and Ce/P point to sediment-derived (S-type) magma and high values to A-type (Sawaki et al. 2022); P against REE+Y follows a 1:1 molar trend in S-type zircon (Burnham and Berry 2017).",
        "ΣREE+Y is exported in µg/g, whereas the P–REE+Y test uses molar amounts; P is hard to measure by quadrupole ICP-MS and apatite inclusions raise it."),
    GroupInfo("Lu et al. (2016); Pizarro et al. (2020)", "literature", "Porphyry Cu fertility", ("lu2016", "pizarro2020"),
        "High 10,000(Eu/Eu*)/Y (> 1) and (Ce/Nd)/Y (> 0.01) and low Dy/Yb (< 0.3) mark hydrous, oxidized arc magmas related to porphyry Cu deposits.",
        "Empirical thresholds from specific belts; Eu/Eu* is sensitive to LREE-rich inclusions."),
    GroupInfo("Roberts et al. (2024)", "literature", "S-type screen", ("roberts2024",),
        "Ce/U plotted against Th/U (Composition group) separates zircon from metasedimentary (S-type) melts, which have low LREE/U and Th/U.",
        "Not an oxidation index; metamorphic and hydrothermal zircon can also have low Th/U."),
    GroupInfo("Loucks et al. (2024)", "literature", "Hydration and oxidation (Cu fertility)", ("loucks2024",),
        "U_i restores U to the crystallization age for the Ce/√(U_i·Ti) oxybarometer; 10,000(Eu/Eu*)/Yb_N tracks magmatic water content.",
        "Th_i and the ×1000 scale of (Ce/Nd)_N/Y follow the UPbR26plus spreadsheet, not the paper (which plots (Ce/Nd)_N/Y after Lu et al. 2016); Ce/√(U_i·Ti) itself is not exported yet."),
    GroupInfo("Ti-in-zircon", "thermometry", "Ti-in-zircon temperature", ("ferry2007",),
        "Zircon crystallization temperature from its Ti content.",
        "Temperatures shift with the assumed activities; the calibration has no pressure term and about 50 °C per GPa of pressure uncertainty (Ferry and Watson 2007)."),
    GroupInfo("Zr-in-rutile", "thermometry", "Zr-in-rutile temperature", ("kohn2020", "ferry2007"),
        "Rutile crystallization or peak temperature from its Zr content.",
        "Requires rutile with zircon and quartz; Zr can be reset by diffusion during slow cooling."),
    GroupInfo("Zr-in-titanite", "thermometry", "Zr-in-titanite temperature", ("hayden2008",),
        "Titanite crystallization temperature from its Zr content.",
        "Strongly pressure-dependent; requires an independent pressure estimate and assumed aTiO2, aSiO2."),
)})


def group_info(group: str) -> GroupInfo:
    """Return purpose notes, falling back to an unreferenced core group."""
    return GROUPS.get(group, GroupInfo(group, "core", group, (), "", ""))


def reference(key: str) -> Reference:
    """Look up a reference by its stable key."""
    return REFERENCES[key]


__all__ = ["Reference", "GroupInfo", "REFERENCES", "GROUPS", "group_info", "reference"]
