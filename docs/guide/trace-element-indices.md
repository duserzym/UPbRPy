# What the trace-element groups and thermometers are for

The **Export results** dialog offers groups of trace-element columns named
after the papers that introduced them, and one crystallization thermometer
for the session's mineral. This page explains, in plain terms, what each one
is meant to tell you and where it can mislead. The exact formulas and
constants are in the [table contract](../export_table_spec.md). Every
exported table repeats the full citation and the values assumed in its
footnotes.

All ratios are mass ratios of µg/g concentrations unless the name carries
the subscript _N, which means divided by CI chondrite (Sun and McDonough 1989).
These indices are empirical. Each was calibrated on particular rock suites,
and none should be used alone.

## Tectonic setting: Grimes et al. (2015)

**Columns:** Nb/Yb, Ce/Yb, Gd/Yb, U/Yb.

**What it is for.** Zircon that grew from mantle-derived magma at mid-ocean
ridges and ocean islands lies on a "mantle-zircon array" in U/Yb against
Nb/Yb. Zircon from continental arcs is offset to higher U/Yb, because arc
magmas are richer in U relative to Nb.

**Caveats.** The fields overlap. They describe igneous zircon only.
Inherited, metamorphic or hydrothermal zircon can fall anywhere. The paper
also uses Sc/Yb to separate arc from plume sources. Studio does not export
Sc/Yb yet.

*Grimes, Wooden, Cheadle and John (2015), Contrib. Mineral. Petrol. 170, 46,
doi:10.1007/s00410-015-1199-3.*

## Granite type (I, S, A): Itano and Sawada (2024)

**Columns:** Nb/P, Ce/P, ΣREE+Y.

**What it is for.** These columns help tell whether the magma came from
melted sediment (S-type), from igneous crust or mantle (I-type and M-type),
or is alkaline and within-plate (A-type).

- **Nb/P and Ce/P.** Magma made from sediment is rich in phosphorus, so its
  zircon has low Nb/P and Ce/P. A-type zircon has high values. These ratios
  come from Sawaki et al. (2022).
- **ΣREE+Y against P.** In S-type zircon, phosphorus and REE+Y rise together
  in a 1:1 molar trend (Burnham and Berry 2017).

Itano and Sawada (2024) plot both diagrams (their Fig. 10) and use machine
learning to classify zircon source rocks. The UPbR26plus spreadsheet labels
this group "Itano '24", and that label refers to this paper.

**Caveats.**

- Phosphorus is hard to measure by quadrupole ICP-MS.
- Apatite inclusions raise P.
- The 1:1 test uses molar amounts, but this column is ΣREE+Y in µg/g.
  Convert it to moles before drawing a 1:1 line.
- Roberts et al. (2024) show that the P test often fails to identify S-type
  granites.

*Itano and Sawada (2024), Math. Geosci. 56, 1139–1160,
doi:10.1007/s11004-023-10128-z; Sawaki, Asanuma, Sakata, Abe and Ohno (2022),
Island Arc 31, e12466, doi:10.1111/iar.12466; Burnham and Berry (2017),
Nat. Geosci. 10, 457–461, doi:10.1038/ngeo2942.*

## S-type screen: Roberts et al. (2024)

**Columns:** Ce/U, used together with Th/U from the Composition group.

**What it is for.** Melts of metasedimentary rocks are poor in light REE and
Th relative to U. On a plot of Ce/U against Th/U, zircon from S-type granites
falls at low values of both.

**Caveats.** Ce/U is not an oxidation or fertility index here.
Metamorphic and hydrothermal zircon can also have low Th/U.

*Roberts, Yakymchuk, Spencer, Keller and Tapster (2024), Earth Planet. Sci.
Lett. 633, 118638, doi:10.1016/j.epsl.2024.118638.*

## Porphyry copper fertility: Lu et al. (2016) and Pizarro et al. (2020)

**Columns:** Dy/Yb, 10,000(Eu/Eu*)/Y, (Ce/Nd)/Y.

**What it is for.** Arc magmas that form porphyry copper deposits are wet and
oxidized. Water suppresses early plagioclase crystallization, so Eu stays in
the melt and zircon has a small Eu anomaly. Amphibole crystallizes early and
removes middle REE.

Zircon from such magmas tends to have:

- 10,000(Eu/Eu*)/Y > 1;
- (Ce/Nd)/Y > 0.01;
- Dy/Yb < 0.3.

Lu et al. (2016) defined these thresholds. Pizarro et al. (2020) applied
them, with Hf, Ce/Nd and Eu/Eu*, as "porphyry indicator zircon" criteria
for exploration.

**Caveats.** The thresholds come from particular belts. Small LREE-rich
inclusions inflate Ce, Nd and Eu*. These ratios rank prospects; they do not
predict ore.

*Lu et al. (2016), SEG Spec. Publ. 19, 329–347, doi:10.5382/SP.19.13;
Pizarro et al. (2020), Ore Geol. Rev. 126, 103771,
doi:10.1016/j.oregeorev.2020.103771.*

## Hydration and oxidation: Loucks et al. (2024)

**Columns:** U_i, Th_i, 10,000(Eu/Eu*)/Yb_N, 1000(Ce/Nd)_N/Y.

**What it is for.** Loucks et al. combine a hydration index and an oxidation
index to separate copper-ore-forming arc magmas from barren ones.

- **Hydration index.** 10,000(Eu/Eu*)/Yb_N.
- **Oxidation index.** The oxybarometer Ce/√(U_i·Ti).
- **U_i.** U_i is the U content at crystallization. U has decayed since the
  zircon grew, so the measured U is restored using the preferred date.

**Caveats.**

- Studio does not export Ce/√(U_i·Ti) yet.
- Th_i, which is Th restored for decay, does not come from the paper.
- The ×1000 scale on (Ce/Nd)_N/Y does not come from the paper either. Both
  follow the UPbR26plus spreadsheet.
- The paper plots (Ce/Nd)_N/Y after Lu et al. (2016).

*Loucks, Henríquez and Fiorentini (2024), Econ. Geol. 119, 511–523,
doi:10.5382/econgeo.5071.*

## Thermometers

Studio shows only the thermometer for the session's mineral: Ti in zircon,
Zr in rutile, or Zr in titanite. Each thermometer gives the temperature at
which the crystal grew, or last equilibrated, from one trace element.

Every thermometer depends on how much SiO₂ and TiO₂ the melt or rock held,
expressed as activities (aSiO₂, aTiO₂). It may also depend on pressure. You
enter these values in the dialog, and the footnote records them.

- **Activity of 1.** An activity of 1 means the mineral was present: quartz
  for aSiO₂, rutile for aTiO₂.
- **Lower activity.** A lower activity means the melt was undersaturated in
  that oxide.
- **Effect on results.** Wrong activities shift every temperature in the
  same direction, so a series of samples keeps its order.

### Ti-in-zircon: Ferry and Watson (2007)

log(Ti, ppm) = 5.711 − 4800/T − log aSiO₂ + log aTiO₂, with T in kelvin.

The defaults are aSiO₂ = 1 (quartz present) and aTiO₂ = 0.6. Most silicic
magmas lack rutile, and their aTiO₂ is usually 0.5–0.7. The calibration has
no pressure term. Each GPa of pressure uncertainty adds about 50 °C of
temperature uncertainty.

*Contrib. Mineral. Petrol. 154, 429–437, doi:10.1007/s00410-007-0201-0.*

### Zr-in-rutile: Kohn (2020), default, or Ferry and Watson (2007)

**Kohn (2020):**
T = (71360 + 0.378·P − 0.130·C) / (130.66 − R·ln C).

- T is in kelvin, P in bar (entered in GPa), C is Zr in ppm, and
  R = 8.314 J/(mol·K).
- It assumes rutile grew with zircon and quartz, so aSiO₂ = 1.
- It refines earlier calibrations, includes pressure, and quotes ±10–15 °C.

**Ferry and Watson (2007):**
log(Zr, ppm) = 7.420 − 4530/T − log aSiO₂.

This calibration has no pressure term.

Zr diffuses fast in rutile, so slowly cooled rocks can give reset, lower
temperatures.

*Kohn (2020), Am. Mineral. 105, 963–971, doi:10.2138/am-2020-7091.*

### Zr-in-titanite: Hayden, Watson and Wark (2008)

log(Zr, ppm) = 10.52 − 7708/T − 960·P/T − log aTiO₂ − log aSiO₂, with T in
kelvin and P in GPa.

This thermometer depends strongly on pressure, so it needs an independent
pressure estimate. The paper appeared online in 2007, which is why the old
label said 2007, but the volume year is 2008.

*Contrib. Mineral. Petrol. 155, 529–540, doi:10.1007/s00410-007-0256-y.*
