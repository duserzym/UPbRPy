"""Project display colours, independent of Qt and reduction calculations.

Samples use sorted session names; spots use acquisition order. Filtering views
never changes either order. Overrides apply in both sample and spot modes;
single mode always uses the supplied theme accent. Depth time maps and isotope
channel colours are separate display concepts.
"""
from dataclasses import dataclass, field
import re

SPOT_PALETTE = [
    "#5590f5", "#f5c040", "#f07840", "#b864f0", "#40c870",
    "#d04848", "#40c8d8", "#f060a8", "#b8d840", "#f0a040",
    "#7090d8", "#d090f0",
]

SAMPLE_PALETTE = [
    "#4477aa", "#ee7733", "#228833", "#cc6677", "#aa4499", "#66aabb",
    "#aa8833", "#44aa99", "#dd6699", "#8888cc", "#999933", "#bb5566",
    "#3366cc", "#cc4422", "#7f55aa", "#669944", "#008899", "#b07850",
    "#6f7d8c", "#bb55bb",
]



def normalise_colour(value: str) -> str:
    """Validate an opaque RGB colour and return lower-case #rrggbb."""
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("Expected a colour in #rrggbb form")
    return value.lower()


@dataclass
class SpotColours:
    """Resolve project overrides against the unchanged Studio palettes."""

    sample_colours: dict[str, str] = field(default_factory=dict)
    spot_colours: dict[str, str] = field(default_factory=dict)
    _samples: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _spots: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def set_order(self, samples, spots):
        """Set the complete session order, independent of displayed subsets."""
        self._samples = {name: SAMPLE_PALETTE[i % len(SAMPLE_PALETTE)]
                         for i, name in enumerate(sorted(set(map(str, samples))))}
        self._spots = {name: SPOT_PALETTE[i % len(SPOT_PALETTE)]
                       for i, name in enumerate(dict.fromkeys(map(str, spots)))}

    def sample_colour(self, sample: str) -> str:
        """Return a user sample colour or its stable session palette entry."""
        return self.sample_colours.get(sample, self._samples.get(sample, SAMPLE_PALETTE[0]))

    def spot_colour(self, spot: str, sample: str, mode: str, *, accent: str = "#4477aa") -> str:
        """Resolve sample/spot overrides; single mode ignores all overrides."""
        if mode == "single":
            return accent
        if mode not in {"sample", "spot"}:
            raise ValueError(f"Unknown colour mode: {mode}")
        return self.spot_colours.get(spot, self.sample_colour(sample) if mode == "sample"
                                     else self._spots.get(spot, SPOT_PALETTE[0]))

    def restore(self, samples, spots):
        """Restore valid known overrides, ignoring unknown names and bad values."""
        def known(values, defaults):
            result = {}
            if isinstance(values, dict):
                for name, value in values.items():
                    if name in defaults:
                        try:
                            result[name] = normalise_colour(value)
                        except ValueError:
                            pass
            return result
        self.sample_colours = known(samples, self._samples)
        self.spot_colours = known(spots, self._spots)
