"""General-purpose time-series visualization helpers."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import inspect
from functools import wraps
from html import escape
from pathlib import Path
import re
import tempfile

from bokeh.embed import components
from bokeh.models import (
    BoxAnnotation,
    Button as BokehButton,
    CDSView,
    ColumnDataSource,
    CustomJS,
    Div,
    HoverTool,
    IndexFilter,
    RadioButtonGroup as BokehRadioButtonGroup,
    Range1d,
    SaveTool,
    Select as BokehSelect,
)
from bokeh.layouts import (
    column as bokeh_column,
    gridplot as bokeh_gridplot,
    row as bokeh_row,
)
from bokeh.plotting import figure
from bokeh.resources import CDN
import numpy as np
import pandas as pd

from .constants import common_U238_U235, lambda_235, lambda_238
from .io import SelectionError, select_signal_regions
from .ratios import excel_plateau_inlier_mask, normalize_plateau_filter_method


class StandaloneBokehPlot:
    """Notebook-display wrapper for a Bokeh plot without a live Python comm.

    The wrapper serializes the plot as standalone Bokeh HTML when a notebook
    frontend requests its rich representation. Pan, zoom, hover, legend
    toggles, and ``CustomJS`` callbacks continue to work in the browser, while
    the kernel does not retain an ipywidget model or communication channel.

    The original plot remains available through :attr:`object`, matching the
    useful part of the ``Panel`` Bokeh-pane interface and allowing the existing
    vector exporters to operate without special cases.
    """

    __slots__ = ("object",)

    def __init__(self, plot):
        self.object = plot

    def _repr_html_(self):
        """Return a standalone notebook HTML fragment for this plot."""
        script, div = components(self.object)
        return f"{standalone_bokeh_resources()}\n{div}\n{script}"


def standalone_bokeh_resources() -> str:
    """Return only the absolute BokehJS resource needed by read-only plots.

    Panel registers extension assets in Bokeh's global resource collection.
    Some of those assets use paths relative to a running Panel server, which
    notebook frontends such as VS Code incorrectly resolve beside the
    notebook file. Standalone UPbRPy figures contain native Bokeh models only,
    so they need BokehJS core but none of Panel's server-relative resources.

    Filtering the URLs also avoids loading the optional Bokeh GL, widgets,
    tables, and MathJax bundles once for every read-only notebook figure.
    """
    core_urls = [
        url
        for url in CDN.js_files
        if str(url).startswith(("https://", "http://"))
        and re.search(r"/bokeh-[0-9][^/]*\.js$", str(url))
    ]
    if len(core_urls) != 1:
        raise RuntimeError(
            "Could not identify exactly one absolute BokehJS core resource."
        )
    source = escape(str(core_urls[0]), quote=True)
    return (
        f'<script src="{source}"></script>\n'
        '<script>Bokeh.set_log_level("warn");</script>'
    )


def initialize_panel_extension(panel_module, comms: str | None = None) -> None:
    """Initialize Panel once, using its current notebook auto-detection.

    Panel 1.8 detects VS Code notebooks and selects its IPyWidget transport.
    Older UPbRPy notebooks explicitly requested the legacy ``"vscode"``
    transport. Treat that value as automatic for backward compatibility while
    still allowing advanced users to request another supported transport.
    """
    if panel_module.extension._loaded:
        return
    if comms is None or str(comms).strip().lower() in {"", "auto", "vscode"}:
        panel_module.extension()
    else:
        panel_module.extension(comms=comms)


def inline_plot_grid(plots, *, ncols: int = 1, merge_tools: bool = False):
    """Combine read-only Bokeh plots into one notebook output document.

    Consolidating related diagnostics substantially reduces the number of
    independent Bokeh documents and JavaScript resource initializations that
    a notebook frontend must manage. Numerical data and the original plots
    are not modified.
    """
    plot_objects = [
        plot.object if isinstance(plot, StandaloneBokehPlot) else plot
        for plot in plots
        if plot is not None
    ]
    if not plot_objects:
        raise ValueError("plots must contain at least one Bokeh plot")
    ncols = int(ncols)
    if ncols < 1:
        raise ValueError("ncols must be positive")
    return StandaloneBokehPlot(
        bokeh_gridplot(plot_objects, ncols=ncols, merge_tools=bool(merge_tools))
    )


def find_single_bokeh_plot(plot_like):
    """Return the single Bokeh plot nested in a result or Panel object."""
    found = []
    visited = set()

    def visit(item):
        if item is None or id(item) in visited:
            return
        visited.add(id(item))
        if hasattr(item, "output_backend") and hasattr(item, "renderers"):
            found.append(item)
            return
        if isinstance(item, Mapping):
            if "plot" in item:
                visit(item["plot"])
            return
        pane_object = getattr(item, "object", None)
        if pane_object is not None:
            visit(pane_object)
        for child in getattr(item, "objects", ()):
            visit(child)
        for child in getattr(item, "children", ()):
            if isinstance(child, tuple):
                child = child[0]
            visit(child)

    visit(plot_like)
    unique = list(dict.fromkeys(found))
    if len(unique) != 1:
        raise ValueError(
            f"Expected exactly one Bokeh plot, but found {len(unique)}. "
            "Pass a Bokeh figure, a Panel Bokeh pane, or a result containing one plot."
        )
    return unique[0]


def create_vector_export_webdriver():
    """Create a headless browser suitable for Bokeh SVG export."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError as error:
        raise ImportError(
            "Vector export requires Selenium. Install with "
            "`pip install 'UPbRPy[export]'`."
        ) from error

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1600,1200")
    try:
        return webdriver.Chrome(options=options)
    except Exception as error:
        raise RuntimeError(
            "Could not start a headless Chromium browser for vector export. "
            "Install Chrome or Chromium and ensure Selenium Manager can locate it."
        ) from error


def export_bokeh_vector(plot_like, filename, *, webdriver=None):
    """Export a Bokeh plot as SVG or vector PDF.

    PDF export uses Bokeh's SVG backend and converts that SVG without
    rasterization. A Chromium browser, Selenium, and CairoSVG are required.
    """
    from bokeh.io import export_svg

    target = Path(filename).expanduser().resolve()
    suffix = target.suffix.lower()
    if suffix not in {".svg", ".pdf"}:
        raise ValueError("Vector export filename must end in .svg or .pdf")
    target.parent.mkdir(parents=True, exist_ok=True)
    plot = find_single_bokeh_plot(plot_like)
    original_backend = plot.output_backend
    owned_webdriver = None
    if webdriver is None:
        owned_webdriver = create_vector_export_webdriver()
        webdriver = owned_webdriver
    plot.output_backend = "svg"
    try:
        if suffix == ".svg":
            export_svg(plot, filename=target, webdriver=webdriver)
        else:
            try:
                import cairosvg
            except ImportError as error:
                raise ImportError(
                    "PDF export requires CairoSVG. Install with "
                    "`pip install 'UPbRPy[export]'`."
                ) from error
            with tempfile.TemporaryDirectory(prefix="upbrpy-vector-") as temporary:
                svg_path = Path(temporary) / f"{target.stem}.svg"
                pdf_path = Path(temporary) / f"{target.stem}.pdf"
                export_svg(plot, filename=svg_path, webdriver=webdriver)
                svg_text = svg_path.read_text(encoding="utf-8")
                # Bokeh emits CSS Color 4's space-separated rgb syntax, while
                # CairoSVG currently expects the equivalent legacy syntax.
                svg_text = re.sub(
                    r"rgb\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*/\s*([\d.]+)\s*\)",
                    r"rgba(\1, \2, \3, \4)",
                    svg_text,
                )
                svg_text = re.sub(
                    r"rgb\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\)",
                    r"rgb(\1, \2, \3)",
                    svg_text,
                )
                cairosvg.svg2pdf(
                    bytestring=svg_text.encode("utf-8"),
                    write_to=str(pdf_path),
                )
                pdf_path.replace(target)
    except RuntimeError as error:
        raise RuntimeError(
            "Bokeh vector export needs Selenium and an installed Chromium browser. "
            "Install the export dependencies with `pip install 'UPbRPy[export]'`."
        ) from error
    finally:
        plot.output_backend = original_backend
        if owned_webdriver is not None:
            owned_webdriver.quit()
    return target


def export_bokeh_plots(plots, output_directory, *, file_format="pdf", webdriver=None):
    """Export a named mapping of Bokeh/Panel plots to vector files."""
    if not isinstance(plots, Mapping):
        raise TypeError("plots must be a mapping of output names to plot objects")
    file_format = str(file_format).lower().lstrip(".")
    if file_format not in {"pdf", "svg"}:
        raise ValueError("file_format must be 'pdf' or 'svg'")
    destination = Path(output_directory).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    exported = {}
    owned_webdriver = None
    if webdriver is None:
        owned_webdriver = create_vector_export_webdriver()
        webdriver = owned_webdriver
    try:
        for name, plot_like in plots.items():
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)).strip("._")
            safe_name = safe_name or "plot"
            target = destination / f"{safe_name}.{file_format}"
            exported[str(name)] = export_bokeh_vector(
                plot_like,
                target,
                webdriver=webdriver,
            )
    finally:
        if owned_webdriver is not None:
            owned_webdriver.quit()
    return exported


def depth_profile_concordia_editor(
    depth_profile,
    *,
    initial_spot=None,
    initial_type="Wetherill",
    output_directory="vector_plots",
    width=1100,
    height=500,
    comms: str | None = None,
):
    """Build a browser-responsive sweep-wise concordia explorer.

    Spot and diagram navigation run through Bokeh ``CustomJS`` callbacks, so
    they continue to work when a notebook's Python comm channel is busy or has
    disconnected. The toolbar Save action downloads the current view as vector
    SVG. Use :func:`export_bokeh_vector` for programmatic PDF export.

    ``output_directory`` and ``comms`` remain accepted for compatibility with
    earlier Panel-backed notebooks. Browser downloads choose their destination
    according to browser settings, while programmatic exports take an explicit
    path.
    """
    from .isoplot import concordia, concordia_plot_ranges

    if not isinstance(depth_profile, pd.DataFrame):
        raise TypeError("depth_profile must be a pandas DataFrame")
    required = {
        "spot_name",
        "206Pb/238U_fractionation_corrected",
        "206Pb/238U_fractionation_corrected_2e",
        "207Pb/206Pb_fractionation_corrected",
        "207Pb/206Pb_fractionation_corrected_2e",
        "207Pb/235U_fractionation_corrected",
        "207Pb/235U_fractionation_corrected_2e",
        "238U/206Pb_fractionation_corrected",
        "238U/206Pb_fractionation_corrected_2e",
        "err_corr",
    }
    missing = sorted(required.difference(depth_profile.columns))
    if missing:
        raise KeyError(f"Depth-profile data are missing columns: {', '.join(missing)}")
    if depth_profile.empty:
        raise ValueError("depth_profile must contain at least one sweep")
    if initial_type not in {"Wetherill", "TW"}:
        raise ValueError("initial_type must be either Wetherill or TW")

    # The viewer needs only tabular columns. Keep its working copy attr-free so
    # ancillary metadata is not repeatedly compared or copied while building
    # the per-spot browser data sources.
    original_attrs = depth_profile.attrs
    try:
        depth_profile.attrs = {}
        profile_data = depth_profile.copy()
    finally:
        depth_profile.attrs = original_attrs
    profile_data.attrs = {}
    if "signal_relative_time" not in profile_data:
        if "Time" in profile_data:
            numeric_time = pd.to_numeric(profile_data["Time"], errors="coerce")
            profile_data["signal_relative_time"] = numeric_time - numeric_time.groupby(
                profile_data["spot_name"]
            ).transform("min")
        else:
            profile_data["signal_relative_time"] = profile_data.groupby(
                "spot_name"
            ).cumcount().astype(float)

    spot_values = list(pd.unique(profile_data["spot_name"]))
    spot_lookup = {str(value): value for value in spot_values}
    initial_label = str(initial_spot) if initial_spot is not None else str(spot_values[0])
    if initial_label not in spot_lookup:
        raise ValueError(f"Unknown initial_spot: {initial_spot!r}")

    spot_selector = BokehSelect(
        title="Spot",
        options=list(spot_lookup),
        value=initial_label,
        width=260,
        name="depth_profile_spot_selector",
    )
    previous_spot_button = BokehButton(
        label="◀ Previous spot",
        width=130,
        name="depth_profile_previous_spot",
    )
    next_spot_button = BokehButton(
        label="Next spot ▶",
        width=130,
        name="depth_profile_next_spot",
    )
    type_selector = BokehRadioButtonGroup(
        labels=["Wetherill", "Tera–Wasserburg"],
        active=0 if initial_type == "Wetherill" else 1,
        button_type="primary",
        width=310,
        name="depth_profile_diagram_selector",
    )
    def numeric_values(frame, column):
        return pd.to_numeric(frame[column], errors="coerce").to_numpy(dtype=float)

    def profile_record(spot_label, diagram_type):
        selected = profile_data.loc[
            profile_data["spot_name"] == spot_lookup[spot_label]
        ]
        if diagram_type == "Wetherill":
            x = numeric_values(selected, "207Pb/235U_fractionation_corrected")
            y = numeric_values(selected, "206Pb/238U_fractionation_corrected")
            sx = numeric_values(
                selected, "207Pb/235U_fractionation_corrected_2e"
            ) / 2
            sy = numeric_values(
                selected, "206Pb/238U_fractionation_corrected_2e"
            ) / 2
            rho_values = np.nan_to_num(
                numeric_values(selected, "err_corr"), nan=0.0
            )
        else:
            x = numeric_values(selected, "238U/206Pb_fractionation_corrected")
            y = numeric_values(selected, "207Pb/206Pb_fractionation_corrected")
            sx = numeric_values(
                selected, "238U/206Pb_fractionation_corrected_2e"
            ) / 2
            sy = numeric_values(
                selected, "207Pb/206Pb_fractionation_corrected_2e"
            ) / 2
            rho_values = np.zeros(len(selected), dtype=float)
        rho_values = np.clip(rho_values, -1, 1)
        xlim, ylim, *_rest = concordia_plot_ranges(
            diagram_type,
            x,
            y,
            sx,
            sy,
            None,
            None,
            None,
            None,
        )
        sample_values = (
            selected["sample"].astype(str).tolist()
            if "sample" in selected
            else [""] * len(selected)
        )
        age_75 = (
            pd.to_numeric(selected["207Pb/235U_age"], errors="coerce").tolist()
            if "207Pb/235U_age" in selected
            else [np.nan] * len(selected)
        )
        age_68 = (
            pd.to_numeric(selected["206Pb/238U_age"], errors="coerce").tolist()
            if "206Pb/238U_age" in selected
            else [np.nan] * len(selected)
        )
        return {
            "spot": spot_label,
            "diagram": diagram_type,
            "x": x.tolist(),
            "y": y.tolist(),
            "sx": sx.tolist(),
            "sy": sy.tolist(),
            "rho": rho_values.tolist(),
            "time": numeric_values(selected, "signal_relative_time").tolist(),
            "sample": sample_values,
            "age_75": age_75,
            "age_68": age_68,
            "x_start": float(xlim[0]),
            "x_end": float(xlim[1]),
            "y_start": float(ylim[0]),
            "y_end": float(ylim[1]),
        }

    records = []
    point_data = {
        "spot_index": [],
        "wetherill_x": [],
        "wetherill_y": [],
        "wetherill_sx": [],
        "wetherill_sy": [],
        "wetherill_rho": [],
        "tw_x": [],
        "tw_y": [],
        "tw_sx": [],
        "tw_sy": [],
        "time": [],
        "age_75": [],
        "age_68": [],
    }
    # Store every sweep once. The former browser source duplicated all points
    # for Wetherill and Tera-Wasserburg views and repeated spot/diagram strings
    # per row, accounting for several megabytes of notebook JSON.
    for spot_index, spot_label in enumerate(spot_lookup):
        wetherill = profile_record(spot_label, "Wetherill")
        tera_wasserburg = profile_record(spot_label, "TW")
        records.extend((wetherill, tera_wasserburg))
        point_count = len(wetherill["x"])
        point_data["spot_index"].extend([spot_index] * point_count)
        for source_name, target_name in (
            ("x", "wetherill_x"),
            ("y", "wetherill_y"),
            ("sx", "wetherill_sx"),
            ("sy", "wetherill_sy"),
            ("rho", "wetherill_rho"),
        ):
            point_data[target_name].extend(wetherill[source_name])
        for source_name, target_name in (
            ("x", "tw_x"),
            ("y", "tw_y"),
            ("sx", "tw_sx"),
            ("sy", "tw_sy"),
        ):
            point_data[target_name].extend(tera_wasserburg[source_name])
        for field in ("time", "age_75", "age_68"):
            point_data[field].extend(wetherill[field])
    for field in point_data:
        point_data[field] = np.asarray(point_data[field])
    profile_source = ColumnDataSource(
        point_data,
        name="depth_profile_switch_data",
    )
    range_fields = ("x_start", "x_end", "y_start", "y_end")
    range_source = ColumnDataSource(
        {
            "spot": [record["spot"] for record in records],
            "diagram": [record["diagram"] for record in records],
            **{
                field: [record[field] for record in records]
                for field in range_fields
            },
        },
        name="depth_profile_switch_ranges",
    )

    initial_selected = profile_data.loc[
        profile_data["spot_name"] == spot_lookup[initial_label]
    ].copy()
    plot = concordia(
            initial_selected,
            type=initial_type,
            r75="207Pb/235U_fractionation_corrected",
            r68="206Pb/238U_fractionation_corrected",
            r86="238U/206Pb_fractionation_corrected",
            r76="207Pb/206Pb_fractionation_corrected",
            s75="207Pb/235U_fractionation_corrected_2e",
            s68="206Pb/238U_fractionation_corrected_2e",
            s86="238U/206Pb_fractionation_corrected_2e",
            s76="207Pb/206Pb_fractionation_corrected_2e",
            rho="err_corr",
            color_by="signal_relative_time",
            color_key="legend",
            ellipse_fill_alpha=0.35,
            ellipse_center_size=6,
        )
    plot.width = int(width)
    plot.height = int(height)
    plot.name = "depth_profile_concordia_plot"
    plot.title.text = f"{initial_label} | {initial_type} depth profile"
    plot.output_backend = "svg"
    save_tools = list(plot.select(type=SaveTool))
    if save_tools:
        save_tools[0].filename = re.sub(
            r"[^A-Za-z0-9._-]+",
            "_",
            f"{initial_label}_{initial_type}_depth_profile.svg",
        ).strip("._")

    ellipse_source = plot.select(name="concordia_ellipse_source")[0]
    center_source = plot.select(name="concordia_center_source")[0]
    curve_source = plot.select(name="full_earth_concordia_source")[0]
    label_source = plot.select(name="concordia_age_labels")[0]
    color_mapper = plot.select(name="concordia_color_mapper")[0]

    switch_callback = CustomJS(
        args={
            "profiles": profile_source,
            "ranges": range_source,
            "spot_selector": spot_selector,
            "type_selector": type_selector,
            "ellipses": ellipse_source,
            "centers": center_source,
            "curve": curve_source,
            "age_labels": label_source,
            "mapper": color_mapper,
            "plot": plot,
            "x_axis": plot.xaxis[0],
            "y_axis": plot.yaxis[0],
            "save_tool": save_tools[0] if save_tools else None,
            "lambda235": lambda_235,
            "lambda238": lambda_238,
            "uranium_ratio": common_U238_U235,
        },
        code="""
const diagram = type_selector.active === 0 ? "Wetherill" : "TW";
const selectedSpotIndex = spot_selector.options.indexOf(spot_selector.value);
const spotIndices = profiles.data.spot_index;
const xValues = diagram === "Wetherill" ? profiles.data.wetherill_x : profiles.data.tw_x;
const yValues = diagram === "Wetherill" ? profiles.data.wetherill_y : profiles.data.tw_y;
const sxValues = diagram === "Wetherill" ? profiles.data.wetherill_sx : profiles.data.tw_sx;
const syValues = diagram === "Wetherill" ? profiles.data.wetherill_sy : profiles.data.tw_sy;
const rhoValues = diagram === "Wetherill" ? profiles.data.wetherill_rho : null;
const x = [];
const y = [];
const sx = [];
const sy = [];
const rho = [];
const time = [];
const sample = [];
const age75 = [];
const age68 = [];
for (let i = 0; i < spotIndices.length; i += 1) {
  if (spotIndices[i] === selectedSpotIndex) {
    x.push(xValues[i]);
    y.push(yValues[i]);
    sx.push(sxValues[i]);
    sy.push(syValues[i]);
    rho.push(rhoValues == null ? 0 : rhoValues[i]);
    time.push(profiles.data.time[i]);
    sample.push(spot_selector.value);
    age75.push(profiles.data.age_75[i]);
    age68.push(profiles.data.age_68[i]);
  }
}
if (x.length === 0) return;
let rangeIndex = -1;
for (let i = 0; i < ranges.data.spot.length; i += 1) {
  if (ranges.data.spot[i] === spot_selector.value &&
      ranges.data.diagram[i] === diagram) {
    rangeIndex = i;
    break;
  }
}
if (rangeIndex < 0) return;
const polygonXs = [];
const polygonYs = [];
const polygonTime = [];
const cutoff = 5.991464547107979;
for (let i = 0; i < x.length; i += 1) {
  if (![x[i], y[i], sx[i], sy[i], rho[i]].every(Number.isFinite)) continue;
  const a = sx[i] * sx[i];
  const d = sy[i] * sy[i];
  const b = rho[i] * sx[i] * sy[i];
  const discriminant = Math.sqrt(Math.max(0, (a - d) ** 2 + 4 * b * b));
  const major = Math.max(0, 0.5 * (a + d + discriminant));
  const minor = Math.max(0, 0.5 * (a + d - discriminant));
  const majorRadius = Math.sqrt(cutoff * major);
  const minorRadius = Math.sqrt(cutoff * minor);
  const angle = 0.5 * Math.atan2(2 * b, a - d);
  const ellipseX = [];
  const ellipseY = [];
  for (let point = 0; point < 60; point += 1) {
    const theta = 2 * Math.PI * point / 59;
    ellipseX.push(x[i] + majorRadius * Math.cos(theta) * Math.cos(angle) -
      minorRadius * Math.sin(theta) * Math.sin(angle));
    ellipseY.push(y[i] + majorRadius * Math.cos(theta) * Math.sin(angle) +
      minorRadius * Math.sin(theta) * Math.cos(angle));
  }
  polygonXs.push(ellipseX);
  polygonYs.push(ellipseY);
  polygonTime.push(time[i]);
}
ellipses.data = {xs: polygonXs, ys: polygonYs, value: polygonTime};
centers.data = {
  x: x,
  y: y,
  sample: sample,
  age_75: age75,
  age_68: age68,
};
const finiteTime = time.filter(Number.isFinite);
const low = finiteTime.length ? Math.min(...finiteTime) : 0;
const high = finiteTime.length ? Math.max(...finiteTime) : 1;
mapper.low = low;
mapper.high = high > low ? high : low + 1;

const ages = curve.data.age;
const curveX = [];
const curveY = [];
for (const age of ages) {
  const years = age * 1e6;
  const pb68 = Math.exp(lambda238 * years) - 1;
  const pb75 = Math.exp(lambda235 * years) - 1;
  if (diagram === "Wetherill") {
    curveX.push(pb75);
    curveY.push(pb68);
  } else {
    curveX.push(1 / pb68);
    curveY.push(pb75 / pb68 / uranium_ratio);
  }
}
curve.data = {age: ages, x: curveX, y: curveY};
age_labels.data = {x: [], y: [], label: []};
plot.x_range.start = ranges.data.x_start[rangeIndex];
plot.x_range.end = ranges.data.x_end[rangeIndex];
plot.y_range.start = ranges.data.y_start[rangeIndex];
plot.y_range.end = ranges.data.y_end[rangeIndex];
x_axis.axis_label = diagram === "Wetherill" ? "207Pb/235U" : "238U/206Pb";
y_axis.axis_label = diagram === "Wetherill" ? "206Pb/238U" : "207Pb/206Pb";
plot.title.text = `${spot_selector.value} | ${diagram} depth profile`;
if (save_tool != null) {
  const safeSpot = spot_selector.value.replace(/[^A-Za-z0-9._-]+/g, "_");
  save_tool.filename = `${safeSpot}_${diagram}_depth_profile.svg`;
}
ellipses.change.emit();
centers.change.emit();
curve.change.emit();
age_labels.change.emit();
plot.change.emit();
""",
    )
    spot_selector.js_on_change("value", switch_callback)
    type_selector.js_on_change("active", switch_callback)

    previous_spot_button.js_on_click(
        CustomJS(
            args={"selector": spot_selector},
            code="""
const index = selector.options.indexOf(selector.value);
selector.value = selector.options[(index - 1 + selector.options.length) % selector.options.length];
""",
        )
    )
    next_spot_button.js_on_click(
        CustomJS(
            args={"selector": spot_selector},
            code="""
const index = selector.options.indexOf(selector.value);
selector.value = selector.options[(index + 1) % selector.options.length];
""",
        )
    )

    browser_status = Div(
        text=(
            "Spot and diagram controls run locally in the browser and do not "
            "depend on the notebook kernel. Use the plot toolbar's Save action "
            "to download the current view as vector SVG."
        ),
        styles={"color": "#555", "font-size": "12px"},
    )
    bokeh_controls = bokeh_row(
        previous_spot_button,
        spot_selector,
        next_spot_button,
        type_selector,
    )
    interactive_layout = bokeh_column(bokeh_controls, browser_status, plot)
    return StandaloneBokehPlot(interactive_layout)


def initialize_bokeh():
    """Initialize Bokeh output in an active notebook, when available."""
    try:
        from bokeh.io import output_notebook

        output_notebook(hide_banner=True)
    except Exception:
        # Plot objects still render through their rich representation in
        # notebook frontends that manage Bokeh resources themselves.
        pass


def inline_correction_plots(results: Mapping[str, Mapping[str, object]]):
    """Return correction results with standalone inline Bokeh plots.

    The result and per-ratio dictionaries are shallow-copied; numerical fits,
    interpolation callables, and metadata retain their original objects. The
    display wrapper uses browser-side JavaScript and does not allocate a live
    notebook widget for each read-only correction plot.
    """
    if not isinstance(results, Mapping):
        raise TypeError("correction results must be a mapping keyed by isotope ratio.")

    inline_results = {}
    for ratio_name, ratio_result in results.items():
        if not isinstance(ratio_result, Mapping):
            raise TypeError(f"Correction result {ratio_name!r} must be a mapping.")
        inline_result = dict(ratio_result)
        correction_plot = inline_result.get("plot")
        if correction_plot is not None and not isinstance(
            correction_plot, StandaloneBokehPlot
        ):
            inline_result["plot"] = StandaloneBokehPlot(correction_plot)
        inline_results[str(ratio_name)] = inline_result
    return inline_results


def inline_plot_pair(result):
    """Wrap a calculation's read-only plot for standalone inline display."""
    if not isinstance(result, tuple) or len(result) != 2:
        raise TypeError("inline plot results must be a two-item tuple.")
    calculation, result_plot = result
    if result_plot is not None and not isinstance(result_plot, StandaloneBokehPlot):
        result_plot = StandaloneBokehPlot(result_plot)
    return calculation, result_plot


def initialize_notebook(
    *,
    namespace=None,
    comms: str | None = None,
    display_max_points: int | None = 6000,
):
    """Enable efficient inline UPbRPy plots and live editors in a notebook.

    Read-only plots are serialized as standalone Bokeh HTML, so their browser
    interactions do not require persistent kernel communication. Selection and
    depth-profile editors remain Panel-backed because applying selections and
    exporting a current view require Python callbacks. Call this function after
    importing UPbRPy; wrapped functions retain their public call signatures and
    result-dictionary structure.
    """
    try:
        import panel as pn
        __import__("jupyter_bokeh.widgets")
    except ImportError as error:
        raise ImportError(
            "Notebook display support requires Panel and jupyter_bokeh. "
            "Install with `pip install 'UPbRPy[notebook]'`."
        ) from error

    if namespace is None:
        caller = inspect.currentframe().f_back
        if caller is None:
            raise RuntimeError("Pass the notebook namespace explicitly.")
        namespace = caller.f_globals

    from .fractionation import (
        calc_DH_fractionations_rad,
        calc_DH_fractionations_TW,
        calc_session_fractionation_rad,
        calc_session_fractionation_TW,
    )
    from .concentration import calc_concentration_calibration_factor
    from .isoplot import concordia

    if display_max_points is not None:
        display_max_points = int(display_max_points)
        if display_max_points < 4:
            raise ValueError("display_max_points must be at least 4 or None.")

    initialize_panel_extension(pn, comms)

    def wrap_plot(function, *, limit_display_data=False):
        @wraps(function)
        def display_plot(*args, **kwargs):
            if limit_display_data:
                kwargs.setdefault("max_display_points", display_max_points)
            return StandaloneBokehPlot(function(*args, **kwargs))

        return display_plot

    def wrap_correction_results(function):
        @wraps(function)
        def display_results(*args, **kwargs):
            return inline_correction_plots(function(*args, **kwargs))

        return display_results

    def wrap_plot_pair(function):
        @wraps(function)
        def display_result(*args, **kwargs):
            return inline_plot_pair(function(*args, **kwargs))

        return display_result

    def wrap_plot_mapping(function, *plot_keys):
        """Wrap selected Bokeh values without changing a result mapping's API."""

        @wraps(function)
        def display_result(*args, **kwargs):
            result = function(*args, **kwargs)
            if not isinstance(result, Mapping):
                raise TypeError("inline plot results must be a mapping.")

            inline_result = dict(result)
            for key in plot_keys:
                result_plot = inline_result.get(key)
                if result_plot is not None and not isinstance(
                    result_plot, StandaloneBokehPlot
                ):
                    inline_result[key] = StandaloneBokehPlot(result_plot)
            return inline_result

        return display_result

    namespace.update(
        {
            "plot_selected_data": wrap_plot(
                plot_selected_data,
                limit_display_data=True,
            ),
            "plot_series": wrap_plot(plot_series, limit_display_data=True),
            "plot_time_series": wrap_plot(
                plot_time_series,
                limit_display_data=True,
            ),
            "plot_tw_spot_mean_session_projection": wrap_plot(
                plot_tw_spot_mean_session_projection
            ),
            "plot_tw_standard_calibration": wrap_plot_mapping(
                plot_tw_standard_calibration,
                "layout",
            ),
            "concordia": wrap_plot(concordia),
            "calc_concentration_calibration_factor": wrap_plot_pair(
                calc_concentration_calibration_factor
            ),
            "calc_DH_fractionations_rad": wrap_correction_results(
                calc_DH_fractionations_rad
            ),
            "calc_DH_fractionations_TW": wrap_correction_results(
                calc_DH_fractionations_TW
            ),
            "calc_session_fractionation_rad": wrap_correction_results(
                calc_session_fractionation_rad
            ),
            "calc_session_fractionation_TW": wrap_correction_results(
                calc_session_fractionation_TW
            ),
        }
    )


def selection_editor(
    experiment: pd.DataFrame,
    total_data: pd.DataFrame,
    *,
    primary_channel: str | None = None,
    channels: Iterable[str] | None = None,
    initial_spot: str | None = None,
    width: int = 1080,
    height: int = 430,
    comms: str | None = None,
    signal_duration_limit_seconds: float | None = None,
    baseline_sweep_n: int | None = None,
    peak_sweep_n: int | None = None,
    threshold: float | None = None,
    filter_percentile: float | None = None,
    baseline_offset: int | None = None,
    plateau_offset: int | None = None,
    plateau_filter_method: str = "excel_2sigma",
    on_apply: Callable[
        [pd.DataFrame, str, Mapping[str, tuple[float, float]]], object
    ] | None = None,
):
    """Build an inline editor for baseline and plateau windows.

    The returned Panel view keeps edits pending per spot until its ``Apply``
    button is pressed. Applying writes the selected spot's ordered time bounds,
    plateau-filter method, and filter channel directly to ``experiment``. The
    saved plateau filter is used for isotope-ratio session calibration and
    final spot means, but never for full-signal downhole or depth-profile
    calculations. When
    supplied, ``on_apply`` runs after that committed update and can refresh
    selection-dependent calculations.

    Auto-pick parameters default to the ``selection_parameters`` stored in
    ``experiment.attrs`` by :func:`read_experiment_data`. Explicit keyword
    values override that provenance. Tables created outside the reader retain
    the historical defaults of 10 baseline sweeps and 20 plateau sweeps.
    """
    try:
        import panel as pn

        __import__("jupyter_bokeh.widgets")
    except ImportError as error:
        raise ImportError(
            "Interactive selection editing requires Panel and jupyter_bokeh. "
            "Install with `pip install 'UPbRPy[dev]'`."
        ) from error

    required_experiment_columns = {
        "spot_names",
        "baseline_start_times",
        "baseline_end_times",
        "plateau_start_times",
        "plateau_end_times",
    }
    missing_experiment_columns = required_experiment_columns - set(experiment)
    if missing_experiment_columns:
        missing = ", ".join(sorted(missing_experiment_columns))
        raise KeyError(f"experiment is missing selection columns: {missing}")
    required_data_columns = {"spot_name", "Time"}
    missing_data_columns = required_data_columns - set(total_data)
    if missing_data_columns:
        missing = ", ".join(sorted(missing_data_columns))
        raise KeyError(f"total_data is missing required columns: {missing}")

    spot_names = [str(name) for name in experiment["spot_names"].dropna().unique()]
    if not spot_names:
        raise ValueError("experiment has no spots to edit.")

    numeric_channels = [
        column
        for column in total_data.columns
        if column not in {"Time", "spot_name", "sample"}
        and pd.api.types.is_numeric_dtype(total_data[column])
    ]
    if channels is not None:
        requested_channels = [str(channel) for channel in channels]
        numeric_channels = [
            channel for channel in requested_channels if channel in numeric_channels
        ]
    if not numeric_channels:
        raise ValueError("total_data has no numeric signal channels to display.")
    if primary_channel in numeric_channels:
        numeric_channels.remove(primary_channel)
        numeric_channels.insert(0, primary_channel)

    default_plateau_filter = normalize_plateau_filter_method(
        plateau_filter_method
    )
    default_filter_channel = numeric_channels[0]
    if "plateau_filter_method" not in experiment:
        experiment["plateau_filter_method"] = default_plateau_filter
    else:
        experiment["plateau_filter_method"] = [
            normalize_plateau_filter_method(
                default_plateau_filter if pd.isna(value) else value
            )
            for value in experiment["plateau_filter_method"]
        ]
    if "plateau_filter_channel" not in experiment:
        experiment["plateau_filter_channel"] = default_filter_channel
    else:
        saved_channels = experiment["plateau_filter_channel"].astype("string")
        experiment["plateau_filter_channel"] = saved_channels.where(
            saved_channels.isin(numeric_channels),
            default_filter_channel,
        )

    saved_selection_parameters = experiment.attrs.get(
        "selection_parameters", {}
    )
    if not isinstance(saved_selection_parameters, Mapping):
        saved_selection_parameters = {}

    def auto_pick_parameter(name: str, explicit, fallback, converter):
        value = (
            explicit
            if explicit is not None
            else saved_selection_parameters.get(name, fallback)
        )
        return converter(value)

    auto_pick_parameters = {
        "baseline_sweep_n": auto_pick_parameter(
            "baseline_sweep_n", baseline_sweep_n, 10, int
        ),
        "peak_sweep_n": auto_pick_parameter(
            "peak_sweep_n", peak_sweep_n, 20, int
        ),
        "threshold": auto_pick_parameter(
            "threshold", threshold, 0.1, float
        ),
        "filter_percentile": auto_pick_parameter(
            "filter_percentile", filter_percentile, 70, float
        ),
        "baseline_offset": auto_pick_parameter(
            "baseline_offset", baseline_offset, 2, int
        ),
        "plateau_offset": auto_pick_parameter(
            "plateau_offset", plateau_offset, 2, int
        ),
    }
    if auto_pick_parameters["baseline_sweep_n"] < 1:
        raise ValueError("baseline_sweep_n must be positive.")
    if auto_pick_parameters["peak_sweep_n"] < 1:
        raise ValueError("peak_sweep_n must be positive.")

    initialize_panel_extension(pn, comms)

    source = ColumnDataSource(data={"Time": [], "Signal": []})
    baseline_filter = IndexFilter(indices=[])
    plateau_filter = IndexFilter(indices=[])
    plateau_inlier_filter = IndexFilter(indices=[])
    plateau_outlier_filter = IndexFilter(indices=[])
    plateau_inlier_line_source = ColumnDataSource(data={"xs": [], "ys": []})
    plateau_outlier_line_source = ColumnDataSource(data={"xs": [], "ys": []})
    signal_start_filter = IndexFilter(indices=[])
    signal_end_filter = IndexFilter(indices=[])
    plot = figure(
        width=width,
        height=height,
        x_range=Range1d(0, 1),
        title="Selection editor",
        x_axis_label="Time (s)",
        y_axis_label="cps",
        tools="pan,wheel_zoom,box_zoom,reset,save",
        active_scroll="wheel_zoom",
    )
    plot.line(
        "Time", "Signal", source=source, color="#252a34", line_width=1.5,
        line_alpha=0.42,
        legend_label="Signal trace",
    )
    measurements = plot.scatter(
        "Time",
        "Signal",
        source=source,
        marker="circle",
        size=5,
        fill_color="#252a34",
        line_color="#252a34",
        fill_alpha=0.72,
        line_alpha=0.72,
    )
    plot.add_tools(
        HoverTool(
            renderers=[measurements],
            mode="vline",
            tooltips=[("Time (s)", "@Time{0.000}"), ("Signal", "@Signal{0.000}")],
        )
    )
    baseline_band = BoxAnnotation(
        fill_color="#c43d3d",
        fill_alpha=0.16,
        line_color="#c43d3d",
        line_width=1.5,
        level="underlay",
        editable=True,
        movable="none",
        resizable="x",
        visible=False,
    )
    plateau_band = BoxAnnotation(
        fill_color="#2b74a8",
        fill_alpha=0.16,
        line_color="#2b74a8",
        line_width=1.5,
        level="underlay",
        editable=True,
        movable="none",
        resizable="x",
        visible=False,
    )
    plot.add_layout(baseline_band)
    plot.add_layout(plateau_band)
    plot.scatter(
        "Time",
        "Signal",
        source=source,
        view=CDSView(filter=baseline_filter),
        marker="circle",
        size=7,
        fill_color="#c43d3d",
        line_color="#c43d3d",
        fill_alpha=0.95,
        line_alpha=0.95,
        legend_label="Baseline points",
    )
    plot.multi_line(
        "xs",
        "ys",
        source=plateau_inlier_line_source,
        color="#2b74a8",
        line_width=2,
        line_alpha=0.9,
        legend_label="Plateau included trace",
    )
    plot.multi_line(
        "xs",
        "ys",
        source=plateau_outlier_line_source,
        color="#2b74a8",
        line_width=2,
        line_dash="dashed",
        line_alpha=0.9,
        legend_label="Plateau excluded connection",
    )
    plot.scatter(
        "Time",
        "Signal",
        source=source,
        view=CDSView(filter=plateau_inlier_filter),
        marker="circle",
        size=7,
        fill_color="#2b74a8",
        line_color="#2b74a8",
        fill_alpha=0.95,
        line_alpha=0.95,
        legend_label="Plateau included",
    )
    plot.scatter(
        "Time",
        "Signal",
        source=source,
        view=CDSView(filter=plateau_outlier_filter),
        marker="circle",
        size=8,
        fill_color="white",
        line_color="#2b74a8",
        fill_alpha=1,
        line_alpha=1,
        line_width=2,
        legend_label="Plateau excluded",
    )
    plot.scatter(
        "Time",
        "Signal",
        source=source,
        view=CDSView(filter=signal_start_filter),
        marker="diamond",
        size=17,
        fill_color="#00E676",
        line_color="white",
        line_width=1.5,
        legend_label="Signal start",
    )
    plot.scatter(
        "Time",
        "Signal",
        source=source,
        view=CDSView(filter=signal_end_filter),
        marker="square",
        size=12,
        fill_color="#e07a1f",
        line_color="white",
        line_width=1.5,
        legend_label="Signal end",
    )

    baseline_stats_source = ColumnDataSource(
        data={"Time": [], "Mean": [], "Upper": [], "Lower": []}
    )
    plateau_stats_source = ColumnDataSource(
        data={"Time": [], "Mean": [], "Upper": [], "Lower": []}
    )
    for stats_source, color, label in (
        (baseline_stats_source, "#c43d3d", "Baseline mean"),
        (plateau_stats_source, "#2b74a8", "Plateau mean"),
    ):
        plot.line(
            "Time",
            "Mean",
            source=stats_source,
            color=color,
            line_width=3,
            legend_label=label,
        )
    if plot.legend:
        legend = plot.legend[0]
        legend.orientation = "horizontal"
        legend.location = "center"
        legend.click_policy = "hide"
        legend.label_text_font_size = "8pt"
        plot.add_layout(legend, "below")

    client_filter_code = """
const times = source.data.Time || [];
const values = source.data.Signal || [];
const indices = [];
if (band.left != null && band.right != null) {
  const start = Math.min(band.left, band.right);
  const end = Math.max(band.left, band.right);
  for (let index = 0; index < times.length; index += 1) {
    if (
      Number.isFinite(times[index]) &&
      Number.isFinite(values[index]) &&
      times[index] >= start &&
      times[index] < end
    ) {
      indices.push(index);
    }
  }
}
selection_filter.indices = indices;
selection_filter.change.emit();
"""
    for band, selection_filter in (
        (baseline_band, baseline_filter),
        (plateau_band, plateau_filter),
    ):
        callback = CustomJS(
            args={
                "band": band,
                "selection_filter": selection_filter,
                "source": source,
            },
            code=client_filter_code,
        )
        band.js_on_change("left", callback)
        band.js_on_change("right", callback)
        source.js_on_change("data", callback)

    selected_spot = initial_spot if initial_spot in spot_names else spot_names[0]
    spot_selector = pn.widgets.Select(
        name="Spot",
        options=spot_names,
        value=selected_spot,
    )
    channel_selector = pn.widgets.Select(
        name="Signal channel",
        options=numeric_channels,
        value=numeric_channels[0],
    )
    selection_module_options = {
        "Expanding baseline + σ": "excel_expanding_sigma",
        "Threshold": "threshold",
        "Rolling baseline + σ": "rolling_sigma",
    }
    selection_module_settings = {
        "excel_expanding_sigma": {
            "label": "expanding baseline + σ",
            "method": "excel_expanding_sigma",
        },
        "threshold": {
            "label": "threshold",
            "method": "threshold",
        },
        "rolling_sigma": {
            "label": "rolling baseline + σ",
            "method": "rolling_sigma",
        },
    }
    previous_spot_button = pn.widgets.Button(
        name="Previous spot",
        icon="chevron-left",
    )
    next_spot_button = pn.widgets.Button(
        name="Next spot",
        icon="chevron-right",
    )
    baseline_slider = pn.widgets.RangeSlider(
        name="Baseline window (s)",
        start=0,
        end=1,
        value=(0, 1),
        step=0.01,
    )
    plateau_slider = pn.widgets.RangeSlider(
        name="Plateau window (s)",
        start=0,
        end=1,
        value=(0, 1),
        step=0.01,
    )
    signal_slider = pn.widgets.RangeSlider(
        name="Signal start/end (s)",
        start=0,
        end=1,
        value=(0, 1),
        step=0.01,
    )
    selection_module_selector = pn.widgets.Select(
        name="Selection module",
        options=selection_module_options,
        value="excel_expanding_sigma",
    )
    onset_sigma_input = pn.widgets.FloatInput(
        name="Onset sigma",
        value=8.0,
        start=0.1,
        step=0.5,
    )
    signal_duration_input = pn.widgets.FloatInput(
        name="Max signal duration (s)",
        value=signal_duration_limit_seconds,
        start=0.1,
        step=0.5,
        placeholder="Automatic",
    )
    plateau_filter_selector = pn.widgets.Select(
        name="Plateau filter",
        options={
            "2σ trend filter (95%)": "excel_2sigma",
            "None": "none",
        },
        value=default_plateau_filter,
    )
    auto_baseline_button = pn.widgets.Button(
        name="Auto-pick baseline",
        icon="wand",
    )
    auto_plateau_button = pn.widgets.Button(
        name="Auto-pick plateau",
        icon="wand",
    )
    apply_button = pn.widgets.Button(name="Apply", button_type="primary", icon="check")
    reset_button = pn.widgets.Button(name="Reset", icon="refresh")
    status = pn.pane.Markdown(sizing_mode="stretch_width")
    statistics = pn.pane.Markdown(sizing_mode="stretch_width", margin=(0, 5, 5, 5))
    instructions = pn.pane.Markdown(
        "#### Baseline and plateau windows\n"
        "Drag a colored band edge or set exact limits below. The green diamond and "
        "orange square mark signal start and end; edit them with the signal range slider, "
        "which snaps to measured sweeps and always contains the plateau. Filled plateau "
        "symbols are included; hollow "
        "symbols are filtered out, with dashed connections to adjacent points. Saved "
        "filter exclusions affect isotope-ratio session calibration and final spot means, "
        "but the complete signal remains available to downhole and depth-profile models. "
        "The red "
        "and blue bars show means, and their "
        "shaded rectangles show mean ± 2σ. Max signal duration optionally caps the "
        "auto-picked end relative to signal start; leave it blank for the unrestricted "
        "algorithm. Auto-pick reuses the import's baseline and plateau sweep counts. "
        "Apply commits the selected spot's windows, filter, and filter channel.",
        sizing_mode="stretch_width",
    )

    pending_windows: dict[str, dict[str, tuple[float, float]]] = {}
    current_frame = pd.DataFrame()
    current_spot = selected_spot
    current_signal_indices: tuple[int | None, int | None] = (None, None)
    syncing = False
    selection_feedback: str | None = None
    pending_signal_indices: dict[str, tuple[int, int]] = {}
    pending_filter_settings: dict[str, tuple[str, str]] = {}

    def spot_frame(spot_name: str) -> pd.DataFrame:
        frame = total_data.loc[
            total_data["spot_name"].astype(str) == spot_name
        ].copy()
        return frame.sort_values("Time").reset_index(drop=True)

    def saved_filter_settings(spot_name: str) -> tuple[str, str]:
        row = experiment.loc[
            experiment["spot_names"].astype(str) == spot_name
        ].iloc[0]
        method = normalize_plateau_filter_method(
            row.get("plateau_filter_method", default_plateau_filter)
        )
        channel = str(row.get("plateau_filter_channel", default_filter_channel))
        if channel not in numeric_channels:
            channel = default_filter_channel
        return method, channel

    def current_filter_settings() -> tuple[str, str]:
        return (
            normalize_plateau_filter_method(plateau_filter_selector.value),
            str(channel_selector.value),
        )

    def record_pending_filter_settings() -> None:
        current = current_filter_settings()
        if current == saved_filter_settings(current_spot):
            pending_filter_settings.pop(current_spot, None)
        else:
            pending_filter_settings[current_spot] = current

    def time_limits(frame: pd.DataFrame) -> tuple[float, float, float]:
        times = pd.to_numeric(frame["Time"], errors="coerce").dropna().to_numpy()
        if times.size < 2:
            raise ValueError("A spot needs at least two finite time points to edit.")
        times = np.unique(np.sort(times.astype(float)))
        if times.size < 2 or times[-1] <= times[0]:
            raise ValueError("A spot needs a nonzero time span to edit.")
        spacing = np.diff(times)
        positive_spacing = spacing[spacing > 0]
        step = float(np.median(positive_spacing)) if positive_spacing.size else 0.01
        return float(times[0]), float(times[-1]), max(step, np.finfo(float).eps)

    def normalize_window(
        values: tuple[float, float],
        start_time: float,
        end_time: float,
        step: float,
    ) -> tuple[float, float]:
        start_value, end_value = sorted(float(value) for value in values)
        start_value = min(max(start_value, start_time), end_time)
        end_value = min(max(end_value, start_time), end_time)
        if end_value - start_value >= step:
            return start_value, end_value
        if start_value + step <= end_time:
            return start_value, start_value + step
        return end_time - step, end_time

    def saved_windows(
        spot_name: str,
        frame: pd.DataFrame,
    ) -> dict[str, tuple[float, float]]:
        start_time, end_time, step = time_limits(frame)
        row = experiment.loc[experiment["spot_names"].astype(str) == spot_name].iloc[0]

        def window(start_column: str, end_column: str, fallback: tuple[float, float]):
            values = pd.to_numeric(
                pd.Series([row[start_column], row[end_column]]), errors="coerce"
            ).to_numpy(dtype=float)
            if np.all(np.isfinite(values)) and values[1] > values[0]:
                return normalize_window(tuple(values), start_time, end_time, step)
            return normalize_window(fallback, start_time, end_time, step)

        span = end_time - start_time
        return {
            "baseline": window(
                "baseline_start_times",
                "baseline_end_times",
                (start_time, start_time + span * 0.2),
            ),
            "plateau": window(
                "plateau_start_times",
                "plateau_end_times",
                (end_time - span * 0.2, end_time),
            ),
        }

    def configured_window_values() -> dict[str, tuple[float, float]]:
        start_time, end_time, step = time_limits(current_frame)
        return {
            "baseline": normalize_window(
                tuple(baseline_slider.value), start_time, end_time, step
            ),
            "plateau": normalize_window(
                tuple(plateau_slider.value), start_time, end_time, step
            ),
        }

    def active_selection_options() -> tuple[Mapping[str, object], dict[str, object]]:
        settings = selection_module_settings[str(selection_module_selector.value)]
        method = str(settings["method"])
        options: dict[str, object] = {}
        if method == "excel_expanding_sigma":
            options = {
                "initial_window": 6,
                "sigma_threshold": float(onset_sigma_input.value),
                "consecutive": 1,
            }
        elif method == "rolling_sigma":
            options = {
                "rolling_window": 5,
                "sigma_threshold": float(onset_sigma_input.value),
                "consecutive": 2,
            }
        if signal_duration_input.value is not None:
            options["signal_duration_limit_seconds"] = float(
                signal_duration_input.value
            )
        return settings, options

    def persisted_signal_indices(
        spot_name: str,
        frame: pd.DataFrame,
    ) -> tuple[int | None, int | None]:
        row = experiment.loc[experiment["spot_names"].astype(str) == spot_name].iloc[0]
        times = pd.to_numeric(frame["Time"], errors="coerce").to_numpy(dtype=float)
        if {"signal_start_times", "signal_end_times"}.issubset(experiment.columns):
            saved_times = pd.to_numeric(
                pd.Series([row["signal_start_times"], row["signal_end_times"]]),
                errors="coerce",
            ).to_numpy(dtype=float)
            if np.all(np.isfinite(saved_times)):
                return (
                    int(np.nanargmin(np.abs(times - saved_times[0]))),
                    int(np.nanargmin(np.abs(times - saved_times[1]))),
                )
        return None, None

    def saved_signal_indices(
        spot_name: str,
        frame: pd.DataFrame,
    ) -> tuple[int | None, int | None]:
        if spot_name in pending_signal_indices:
            return pending_signal_indices[spot_name]
        return persisted_signal_indices(spot_name, frame)

    def nearest_signal_indices(
        bounds: tuple[float, float],
    ) -> tuple[int, int]:
        times = pd.to_numeric(current_frame["Time"], errors="coerce").to_numpy(dtype=float)
        finite_indices = np.flatnonzero(np.isfinite(times))
        if finite_indices.size == 0:
            raise ValueError("The active spot has no finite measurement times.")
        indices = [
            int(finite_indices[np.argmin(np.abs(times[finite_indices] - float(bound)))])
            for bound in bounds
        ]
        return min(indices), max(indices)

    def included_window_indices(
        bounds: tuple[float, float],
    ) -> tuple[int, int]:
        """Return first/last sweep in a half-open selection window."""
        times = pd.to_numeric(current_frame["Time"], errors="coerce").to_numpy(dtype=float)
        included = np.flatnonzero(
            np.isfinite(times) & (times >= bounds[0]) & (times < bounds[1])
        )
        if included.size:
            return int(included[0]), int(included[-1])
        return nearest_signal_indices(bounds)

    def signal_times(
        indices: tuple[int | None, int | None],
    ) -> tuple[float, float] | None:
        if any(index is None for index in indices):
            return None
        times = pd.to_numeric(current_frame["Time"], errors="coerce").to_numpy(dtype=float)
        start_index, end_index = (int(index) for index in indices)
        if not (0 <= start_index <= end_index < len(times)):
            return None
        return float(times[start_index]), float(times[end_index])

    def constrain_signal_to_plateau(
        indices: tuple[int, int],
    ) -> tuple[int, int]:
        plateau_bounds = configured_window_values()["plateau"]
        plateau_start_index, plateau_end_index = included_window_indices(
            plateau_bounds
        )
        return (
            min(indices[0], plateau_start_index),
            max(indices[1], plateau_end_index),
        )

    def store_signal_indices(indices: tuple[int, int]) -> None:
        nonlocal current_signal_indices
        current_signal_indices = indices
        persisted = persisted_signal_indices(current_spot, current_frame)
        if indices == persisted:
            pending_signal_indices.pop(current_spot, None)
        else:
            pending_signal_indices[current_spot] = indices

    def expand_signal_to_plateau() -> None:
        nonlocal syncing, selection_feedback
        if any(index is None for index in current_signal_indices):
            return
        indices = tuple(int(index) for index in current_signal_indices)
        constrained = constrain_signal_to_plateau(indices)
        if constrained == indices:
            return
        store_signal_indices(constrained)
        constrained_times = signal_times(constrained)
        if constrained_times is not None:
            syncing = True
            try:
                signal_slider.value = constrained_times
            finally:
                syncing = False
        selection_feedback = "Signal range expanded to contain the plateau window"

    def update_statistics_source(
        stats_source: ColumnDataSource,
        bounds: tuple[float, float],
        indices: list[int],
        values: np.ndarray,
        label: str,
    ) -> tuple[str, float | None, float | None]:
        band = baseline_band if label == "baseline" else plateau_band
        selected_values = values[np.asarray(indices, dtype=int)] if indices else np.array([])
        selected_values = selected_values[np.isfinite(selected_values)]
        if selected_values.size == 0:
            stats_source.data = {"Time": [], "Mean": [], "Upper": [], "Lower": []}
            band.visible = False
            return f"{label}: no finite sweeps", None, None
        mean = float(np.mean(selected_values))
        std = float(np.std(selected_values, ddof=1)) if selected_values.size > 1 else 0.0
        two_sigma = 2 * std
        stats_source.data = {
            "Time": [bounds[0], bounds[1]],
            "Mean": [mean, mean],
            "Upper": [mean + two_sigma, mean + two_sigma],
            "Lower": [mean - two_sigma, mean - two_sigma],
        }
        band.bottom = mean - two_sigma
        band.top = mean + two_sigma
        band.visible = True
        return (
            f"{label}: n={selected_values.size}, mean={mean:.6g}, 2σ={two_sigma:.6g}",
            mean,
            mean + two_sigma,
        )

    def update_selected_points(windows: Mapping[str, tuple[float, float]]) -> None:
        times = pd.to_numeric(current_frame["Time"], errors="coerce").to_numpy(dtype=float)
        values = pd.to_numeric(
            current_frame[channel_selector.value], errors="coerce"
        ).to_numpy(dtype=float)
        valid = np.isfinite(times) & np.isfinite(values)

        def selected_indices(bounds: tuple[float, float]) -> list[int]:
            mask = valid & (times >= bounds[0]) & (times < bounds[1])
            return np.flatnonzero(mask).tolist()

        baseline_indices = selected_indices(windows["baseline"])
        plateau_indices = selected_indices(windows["plateau"])
        plateau_inliers = plateau_indices
        plateau_outliers: list[int] = []
        if plateau_filter_selector.value == "excel_2sigma" and plateau_indices:
            local = np.asarray(plateau_indices, dtype=int)
            local_mask = excel_plateau_inlier_mask(times[local], values[local])
            plateau_inliers = local[local_mask].tolist()
            plateau_outliers = local[~local_mask].tolist()

        inlier_xs: list[list[float]] = []
        inlier_ys: list[list[float]] = []
        outlier_xs: list[list[float]] = []
        outlier_ys: list[list[float]] = []
        outlier_set = set(plateau_outliers)
        for left, right in zip(plateau_indices, plateau_indices[1:]):
            if right != left + 1:
                continue
            segment_x = [float(times[left]), float(times[right])]
            segment_y = [float(values[left]), float(values[right])]
            if left in outlier_set or right in outlier_set:
                outlier_xs.append(segment_x)
                outlier_ys.append(segment_y)
            else:
                inlier_xs.append(segment_x)
                inlier_ys.append(segment_y)
        plateau_inlier_line_source.data = {"xs": inlier_xs, "ys": inlier_ys}
        plateau_outlier_line_source.data = {"xs": outlier_xs, "ys": outlier_ys}

        baseline_filter.indices = baseline_indices
        plateau_filter.indices = plateau_indices
        plateau_inlier_filter.indices = plateau_inliers
        plateau_outlier_filter.indices = plateau_outliers
        start_index, end_index = current_signal_indices
        signal_start_filter.indices = (
            [int(start_index)] if start_index is not None and 0 <= start_index < len(times) else []
        )
        signal_end_filter.indices = (
            [int(end_index)] if end_index is not None and 0 <= end_index < len(times) else []
        )

        baseline_message, _, _ = update_statistics_source(
            baseline_stats_source,
            windows["baseline"],
            baseline_indices,
            values,
            "baseline",
        )
        plateau_message, _, _ = update_statistics_source(
            plateau_stats_source,
            windows["plateau"],
            plateau_inliers,
            values,
            "plateau",
        )
        outlier_note = f", {len(plateau_outliers)} excluded" if plateau_outliers else ""
        statistics.object = (
            f"<span style='color:#a62f2f'><b>{baseline_message}</b></span>"
            " &nbsp;&nbsp; "
            f"<span style='color:#235f91'><b>{plateau_message}{outlier_note}</b></span>"
        )

    def update_status() -> None:
        windows = configured_window_values()
        channel = channel_selector.value
        messages = []
        valid = True
        for name, values in windows.items():
            selected = current_frame.loc[
                (current_frame["Time"] >= values[0])
                & (current_frame["Time"] < values[1])
            ]
            count = len(selected)
            valid &= count > 0
            if name == "plateau" and plateau_filter_selector.value == "excel_2sigma":
                included = len(plateau_inlier_filter.indices)
                excluded = len(plateau_outlier_filter.indices)
                messages.append(
                    f"{name}: {included}/{count} sweeps included ({excluded} excluded)"
                )
            else:
                messages.append(f"{name}: {count} sweeps")
        filter_label = (
            "2σ trend"
            if plateau_filter_selector.value == "excel_2sigma"
            else "none"
        )
        messages.append(f"spot-mean filter: {filter_label}")
        if windows["baseline"][1] > windows["plateau"][0]:
            messages.append("windows overlap")
        is_pending = (
            current_spot in pending_windows
            or current_spot in pending_signal_indices
            or current_spot in pending_filter_settings
        )
        state = "Pending" if is_pending else "Saved"
        spot_position = spot_names.index(current_spot) + 1
        if selection_feedback:
            messages.append(selection_feedback)
        if all(index is not None for index in current_signal_indices):
            times = pd.to_numeric(current_frame["Time"], errors="coerce").to_numpy(dtype=float)
            start_index, end_index = (int(index) for index in current_signal_indices)
            plateau_start_index, plateau_end_index = included_window_indices(
                windows["plateau"]
            )
            contains_plateau = (
                start_index <= plateau_start_index and end_index >= plateau_end_index
            )
            valid &= contains_plateau
            if not contains_plateau:
                messages.append("signal range must contain plateau")
            messages.append(
                f"signal: {times[start_index]:.3f}–{times[end_index]:.3f} s"
            )
        else:
            valid = False
            messages.append("set signal start/end")
        status.object = (
            f"**{state}**: {current_spot} ({spot_position}/{len(spot_names)}) | "
            f"{channel} | " + " | ".join(messages)
        )
        apply_button.disabled = not valid

    def set_band_bounds(windows: Mapping[str, tuple[float, float]]) -> None:
        baseline_band.left, baseline_band.right = windows["baseline"]
        plateau_band.left, plateau_band.right = windows["plateau"]

    def record_pending_windows() -> None:
        windows = configured_window_values()
        saved = saved_windows(current_spot, current_frame)
        equal_to_saved = all(
            np.allclose(windows[name], saved[name]) for name in windows
        )
        if equal_to_saved:
            pending_windows.pop(current_spot, None)
        else:
            pending_windows[current_spot] = windows
        update_selected_points(windows)
        update_status()

    def update_from_sliders(region: str, values: tuple[float, float]) -> None:
        nonlocal syncing, selection_feedback
        if syncing:
            return
        selection_feedback = None
        start_time, end_time, step = time_limits(current_frame)
        bounds = normalize_window(values, start_time, end_time, step)
        syncing = True
        try:
            if region == "baseline":
                baseline_slider.value = bounds
                baseline_band.left, baseline_band.right = bounds
            else:
                plateau_slider.value = bounds
                plateau_band.left, plateau_band.right = bounds
        finally:
            syncing = False
        if region == "plateau":
            expand_signal_to_plateau()
        record_pending_windows()

    def update_signal_from_slider(values: tuple[float, float]) -> None:
        nonlocal current_signal_indices, syncing, selection_feedback
        if syncing or current_frame.empty:
            return
        selection_feedback = None
        requested_indices = nearest_signal_indices(values)
        indices = constrain_signal_to_plateau(requested_indices)
        snapped_times = signal_times(indices)
        if snapped_times is None:
            return
        store_signal_indices(indices)
        if indices != requested_indices:
            selection_feedback = "Signal range constrained to contain the plateau window"
        syncing = True
        try:
            signal_slider.value = snapped_times
        finally:
            syncing = False
        update_selected_points(configured_window_values())
        update_status()

    def update_from_band(region: str) -> None:
        nonlocal syncing, selection_feedback
        if syncing:
            return
        selection_feedback = None
        band = baseline_band if region == "baseline" else plateau_band
        if band.left is None or band.right is None:
            return
        start_time, end_time, step = time_limits(current_frame)
        bounds = normalize_window(
            (float(band.left), float(band.right)), start_time, end_time, step
        )
        syncing = True
        try:
            if region == "baseline":
                baseline_band.left, baseline_band.right = bounds
                baseline_slider.value = bounds
            else:
                plateau_band.left, plateau_band.right = bounds
                plateau_slider.value = bounds
        finally:
            syncing = False
        if region == "plateau":
            expand_signal_to_plateau()
        record_pending_windows()

    def refresh_spot(*_events: object) -> None:
        nonlocal current_frame, current_spot, current_signal_indices, syncing, selection_feedback
        current_spot = str(spot_selector.value)
        current_frame = spot_frame(current_spot)
        selection_feedback = None
        start_time, end_time, step = time_limits(current_frame)
        windows = pending_windows.get(current_spot, saved_windows(current_spot, current_frame))
        filter_method, channel = pending_filter_settings.get(
            current_spot,
            saved_filter_settings(current_spot),
        )
        row = experiment.loc[
            experiment["spot_names"].astype(str) == current_spot
        ].iloc[0]
        saved_error = row.get("selection_error")
        if pd.notna(saved_error) and str(saved_error).strip():
            selection_feedback = f"Auto-pick warning: {saved_error}"
        selected_method = str(row.get("selection_method", ""))
        if selected_method in selection_module_settings:
            selection_module_selector.value = selected_method
        saved_sigma = pd.to_numeric(
            pd.Series([row.get("selection_sigma_threshold")]), errors="coerce"
        ).iloc[0]
        if pd.notna(saved_sigma) and float(saved_sigma) > 0:
            onset_sigma_input.value = float(saved_sigma)

        current_signal_indices = saved_signal_indices(current_spot, current_frame)

        values = pd.to_numeric(current_frame[channel], errors="coerce").to_numpy(dtype=float)
        times = pd.to_numeric(current_frame["Time"], errors="coerce").to_numpy(dtype=float)
        syncing = True
        try:
            channel_selector.value = channel
            plateau_filter_selector.value = filter_method
            for slider, bounds in (
                (baseline_slider, windows["baseline"]),
                (plateau_slider, windows["plateau"]),
            ):
                slider.start = min(float(slider.start), start_time)
                slider.end = max(float(slider.end), end_time)
                slider.value = bounds
                slider.start = start_time
                slider.end = end_time
                slider.step = step
            if all(index is not None for index in current_signal_indices):
                requested_signal_indices = tuple(
                    int(index) for index in current_signal_indices
                )
                constrained_signal_indices = constrain_signal_to_plateau(
                    requested_signal_indices
                )
                if constrained_signal_indices != requested_signal_indices:
                    store_signal_indices(constrained_signal_indices)
                    selection_feedback = (
                        "Signal range expanded to contain the plateau window"
                    )
            current_signal_times = signal_times(current_signal_indices)
            if current_signal_times is None:
                current_signal_times = (start_time, end_time)
            signal_slider.start = min(float(signal_slider.start), start_time)
            signal_slider.end = max(float(signal_slider.end), end_time)
            signal_slider.value = current_signal_times
            signal_slider.start = start_time
            signal_slider.end = end_time
            signal_slider.step = step
            set_band_bounds(windows)
            source.data = {"Time": times.tolist(), "Signal": values.tolist()}
            padding = max(step, (end_time - start_time) * 0.04)
            plot.x_range.start = start_time - padding
            plot.x_range.end = end_time + padding
            plot.title.text = f"{current_spot} | {channel}"
        finally:
            syncing = False
        update_selected_points(windows)
        update_status()

    def refresh_channel(*_events: object) -> None:
        nonlocal syncing, selection_feedback
        if current_frame.empty or syncing:
            return
        selection_feedback = None
        values = pd.to_numeric(
            current_frame[channel_selector.value], errors="coerce"
        ).to_numpy(dtype=float)
        times = pd.to_numeric(current_frame["Time"], errors="coerce").to_numpy(dtype=float)
        source.data = {"Time": times.tolist(), "Signal": values.tolist()}
        plot.title.text = f"{current_spot} | {channel_selector.value}"
        record_pending_filter_settings()
        update_selected_points(configured_window_values())
        update_status()

    def change_spot(offset: int) -> None:
        current_index = spot_names.index(str(spot_selector.value))
        spot_selector.value = spot_names[(current_index + offset) % len(spot_names)]

    def auto_pick_region(region: str) -> None:
        nonlocal current_signal_indices, syncing, selection_feedback
        settings, method_options = active_selection_options()
        values = pd.to_numeric(
            current_frame[channel_selector.value], errors="coerce"
        ).to_numpy(dtype=float)
        times = pd.to_numeric(current_frame["Time"], errors="coerce").to_numpy(dtype=float)
        try:
            selection = select_signal_regions(
                values,
                time=times,
                method=str(settings["method"]),
                **auto_pick_parameters,
                **method_options,
            )
            indices = (
                (selection.baseline_start, selection.baseline_end)
                if region == "baseline"
                else (selection.plateau_start, selection.plateau_end)
            )
            bounds = (float(times[indices[0]]), float(times[indices[1]]))
            start_time, end_time, step = time_limits(current_frame)
            bounds = normalize_window(bounds, start_time, end_time, step)
        except (IndexError, SelectionError, ValueError) as error:
            selection_feedback = (
                f"Could not auto-pick {region} with {settings['label']}: {error}"
            )
            update_status()
            return

        syncing = True
        try:
            if region == "baseline":
                baseline_slider.value = bounds
                baseline_band.left, baseline_band.right = bounds
            else:
                plateau_slider.value = bounds
                plateau_band.left, plateau_band.right = bounds
            selected_signal_times = (
                float(times[selection.signal_start]),
                float(times[selection.signal_end]),
            )
            signal_slider.value = selected_signal_times
        finally:
            syncing = False
        current_signal_indices = (selection.signal_start, selection.signal_end)
        pending_signal_indices[current_spot] = (
            selection.signal_start,
            selection.signal_end,
        )
        selection_feedback = (
            f"Suggested {region} with {settings['label']} and signal edges; "
            "choose Apply to save"
        )
        record_pending_windows()

    def apply_windows(*_events: object) -> None:
        nonlocal selection_feedback
        windows = configured_window_values()
        row_mask = experiment["spot_names"].astype(str) == current_spot
        experiment.loc[row_mask, "baseline_start_times"] = windows["baseline"][0]
        experiment.loc[row_mask, "baseline_end_times"] = windows["baseline"][1]
        experiment.loc[row_mask, "plateau_start_times"] = windows["plateau"][0]
        experiment.loc[row_mask, "plateau_end_times"] = windows["plateau"][1]
        filter_method, filter_channel = current_filter_settings()
        experiment.loc[row_mask, "plateau_filter_method"] = filter_method
        experiment.loc[row_mask, "plateau_filter_channel"] = filter_channel
        start_index, end_index = current_signal_indices
        if start_index is not None and end_index is not None:
            times = pd.to_numeric(current_frame["Time"], errors="coerce").to_numpy(dtype=float)
            experiment.loc[row_mask, "signal_start_times"] = times[int(start_index)]
            experiment.loc[row_mask, "signal_end_times"] = times[int(end_index)]
            experiment.loc[row_mask, "selection_detection_index"] = int(start_index)
            experiment.loc[row_mask, "selection_signal_end_index"] = int(end_index)
            experiment.loc[row_mask, "selection_signal_duration_limit_seconds"] = (
                signal_duration_input.value
            )
            experiment.loc[row_mask, "selection_method"] = str(
                selection_module_settings[str(selection_module_selector.value)]["method"]
            )
            experiment.loc[row_mask, "selection_channel"] = channel_selector.value
            experiment.loc[row_mask, "selection_error"] = None
            if str(selection_module_selector.value) != "threshold":
                experiment.loc[row_mask, "selection_sigma_threshold"] = float(
                    onset_sigma_input.value
                )
        pending_windows.pop(current_spot, None)
        pending_signal_indices.pop(current_spot, None)
        pending_filter_settings.pop(current_spot, None)
        selection_feedback = None
        if on_apply is not None:
            try:
                result = on_apply(experiment, current_spot, windows)
            except Exception as error:
                selection_feedback = f"Saved windows; calculation refresh failed: {error}"
            else:
                if result is not None:
                    selection_feedback = str(result)
        update_status()

    def reset_windows(*_events: object) -> None:
        pending_windows.pop(current_spot, None)
        pending_signal_indices.pop(current_spot, None)
        pending_filter_settings.pop(current_spot, None)
        refresh_spot()

    def update_selection_controls(*_events: object) -> None:
        onset_sigma_input.visible = str(selection_module_selector.value) != "threshold"
        if current_frame.empty or syncing:
            return
        update_status()

    def refresh_plateau_filter(*_events: object) -> None:
        if current_frame.empty or syncing:
            return
        record_pending_filter_settings()
        update_selected_points(configured_window_values())
        update_status()

    spot_selector.param.watch(refresh_spot, "value")
    channel_selector.param.watch(refresh_channel, "value")
    selection_module_selector.param.watch(update_selection_controls, "value")
    onset_sigma_input.param.watch(update_selection_controls, "value")
    signal_duration_input.param.watch(update_selection_controls, "value")
    plateau_filter_selector.param.watch(refresh_plateau_filter, "value")
    baseline_slider.param.watch(
        lambda event: update_from_sliders("baseline", tuple(event.new)), "value"
    )
    plateau_slider.param.watch(
        lambda event: update_from_sliders("plateau", tuple(event.new)), "value"
    )
    signal_slider.param.watch(
        lambda event: update_signal_from_slider(tuple(event.new)), "value"
    )
    for region, band in (("baseline", baseline_band), ("plateau", plateau_band)):
        band.on_change(
            "left", lambda _attr, _old, _new, name=region: update_from_band(name)
        )
        band.on_change(
            "right", lambda _attr, _old, _new, name=region: update_from_band(name)
        )
    apply_button.on_click(apply_windows)
    reset_button.on_click(reset_windows)
    previous_spot_button.on_click(lambda _event: change_spot(-1))
    next_spot_button.on_click(lambda _event: change_spot(1))
    auto_baseline_button.on_click(lambda _event: auto_pick_region("baseline"))
    auto_plateau_button.on_click(lambda _event: auto_pick_region("plateau"))

    update_selection_controls()
    refresh_spot()
    controls = pn.Column(
        spot_selector,
        pn.Row(previous_spot_button, next_spot_button),
        channel_selector,
        baseline_slider,
        plateau_slider,
        signal_slider,
        selection_module_selector,
        onset_sigma_input,
        signal_duration_input,
        plateau_filter_selector,
        pn.Row(auto_baseline_button, auto_plateau_button),
        pn.Row(apply_button, reset_button),
        status,
        width=320,
    )
    return pn.Column(
        instructions,
        pn.Row(
            controls,
            pn.Column(
                statistics,
                pn.pane.Bokeh(plot, width=width, sizing_mode="fixed"),
                width=width,
            ),
        ),
        sizing_mode="stretch_width",
    )


# Retain the former public entry point without retaining a HoloViews dependency.
initialize_holoviews = initialize_bokeh


def extrema_preserving_display_sample(
    dataframe: pd.DataFrame,
    x: str,
    y: str,
    max_points: int | None,
) -> pd.DataFrame:
    """Return a compact, display-only sample that retains local extrema.

    Calculations always continue to use the caller's full DataFrame. For long
    notebook traces, each spot is divided into ordered bins and both the local
    minimum and maximum are retained. This preserves short analytical spikes
    and signal transitions better than fixed-stride sampling.
    """
    columns = [x, y]
    if "spot_name" in dataframe:
        columns.append("spot_name")
    finite = dataframe.loc[:, columns].dropna(subset=[x, y])
    # Reduction provenance may contain DataFrames and arrays whose equality is
    # intentionally undefined. Display sampling does not need that metadata,
    # and clearing it prevents pandas.concat from comparing nested attrs.
    finite.attrs = {}
    if max_points is None or len(finite) <= int(max_points):
        return finite
    max_points = int(max_points)
    if max_points < 4:
        raise ValueError("max_display_points must be at least 4 or None.")

    def sample_one(frame: pd.DataFrame, budget: int) -> pd.DataFrame:
        if len(frame) <= budget or budget < 4:
            return frame
        values = pd.to_numeric(frame[y], errors="coerce").to_numpy(dtype=float)
        bin_count = max((budget - 2) // 2, 1)
        edges = np.linspace(1, len(frame) - 1, bin_count + 1, dtype=int)
        selected = {0, len(frame) - 1}
        for start, stop in zip(edges[:-1], edges[1:]):
            if stop <= start:
                continue
            local = values[start:stop]
            if not np.isfinite(local).any():
                continue
            selected.add(start + int(np.nanargmin(local)))
            selected.add(start + int(np.nanargmax(local)))
        return frame.iloc[sorted(selected)]

    if "spot_name" not in finite:
        return sample_one(finite, max_points)

    groups = list(finite.groupby("spot_name", sort=False, dropna=False))
    # Give every spot enough points to retain its endpoints and extrema, then
    # distribute the remaining display budget in proportion to sweep count.
    minimum_per_spot = 4
    effective_budget = max(max_points, minimum_per_spot * len(groups))
    total_count = len(finite)
    sampled = []
    for _spot_name, group in groups:
        group.attrs = {}
        proportional = round(effective_budget * len(group) / total_count)
        display_group = sample_one(group, max(minimum_per_spot, proportional))
        display_group.attrs = {}
        sampled.append(display_group)
    return pd.concat(sampled).sort_index()


def add_time_series(
    plot,
    dataframe: pd.DataFrame,
    y: str,
    *,
    x: str = "Time",
    color: str = "black",
    kind: str = "line",
    line_width: float = 1,
    size: float = 5,
    alpha: float = 1,
    label: str | None = None,
    max_display_points: int | None = None,
):
    """Add one line or scatter series to an existing Bokeh time-series plot.

    ``max_display_points`` limits only the browser-side plot payload. Local
    extrema and spot boundaries are retained, and the input DataFrame remains
    unchanged for reduction and export calculations.
    """
    if x not in dataframe or y not in dataframe:
        raise KeyError(f"Expected plotting columns {x!r} and {y!r}.")
    display_data = extrema_preserving_display_sample(
        dataframe,
        x,
        y,
        max_display_points,
    )
    source = ColumnDataSource(display_data)
    legend_label = label or y
    if kind == "line":
        renderer = plot.line(
            x=x,
            y=y,
            source=source,
            color=color,
            line_width=line_width,
            alpha=alpha,
            legend_label=legend_label,
        )
    elif kind == "scatter":
        renderer = plot.scatter(
            x=x,
            y=y,
            source=source,
            color=color,
            size=size,
            alpha=alpha,
            legend_label=legend_label,
        )
    else:
        raise ValueError("kind must be 'line' or 'scatter'.")

    tooltips = [(x, f"@{{{x}}}"), (y, f"@{{{y}}}")]
    if "spot_name" in source.data:
        tooltips.append(("spot", "@spot_name"))
    plot.add_tools(HoverTool(renderers=[renderer], tooltips=tooltips))
    return renderer


def plot_series(
    series: Iterable[Mapping[str, object]],
    *,
    x: str = "Time",
    title: str = "Time Series Plot",
    xlabel: str | None = None,
    ylabel: str | None = None,
    width: int = 1200,
    height: int = 400,
    max_display_points: int | None = None,
):
    """Plot multiple labelled DataFrame series on a native Bokeh figure.

    Set ``max_display_points`` to cap the approximate total across rendered
    series with extrema-preserving sampling. A series can override its share
    with a ``max_display_points`` entry in its specification. This changes only
    the notebook visualization, never the underlying reduction data.
    """
    series = list(series)
    shared_point_limit = None
    if max_display_points is not None:
        max_display_points = int(max_display_points)
        if max_display_points < 4:
            raise ValueError("max_display_points must be at least 4 or None.")
        shared_point_limit = max(4, max_display_points // max(len(series), 1))
    plot = figure(
        width=width,
        height=height,
        title=title,
        x_axis_label=xlabel or x,
        y_axis_label=ylabel or "value",
        tools="pan,wheel_zoom,box_zoom,reset,save",
        active_scroll="wheel_zoom",
    )
    for spec in series:
        add_time_series(
            plot,
            spec["data"],
            str(spec["y"]),
            x=str(spec.get("x", x)),
            color=str(spec.get("color", "black")),
            kind=str(spec.get("kind", "line")),
            line_width=float(spec.get("line_width", 1)),
            size=float(spec.get("size", 5)),
            alpha=float(spec.get("alpha", 1)),
            label=str(spec["label"]) if spec.get("label") is not None else None,
            max_display_points=spec.get(
                "max_display_points",
                shared_point_limit,
            ),
        )
    if plot.legend:
        plot.legend.location = "top_right"
        plot.legend.click_policy = "hide"
    return plot

def plot_selected_data(
    experiment,
    total_data,
    channel,
    color="black",
    line_width=0.5,
    width=1200,
    height=400,
    max_display_points: int | None = None,
):
    """Plot one raw channel and highlight every complete saved selection.

    Spots with missing or nonnumeric selection bounds remain visible in the
    raw trace but are omitted from the baseline and plateau overlays. This
    lets users inspect and repair failed auto-picks in mixed-quality sessions.
    """
    required_experiment_columns = {
        "spot_names",
        "baseline_start_times",
        "baseline_end_times",
        "plateau_start_times",
        "plateau_end_times",
    }
    missing_experiment_columns = required_experiment_columns - set(experiment)
    if missing_experiment_columns:
        missing = ", ".join(sorted(missing_experiment_columns))
        raise KeyError(f"experiment is missing selection columns: {missing}")
    required_data_columns = {"spot_name", "Time", channel}
    missing_data_columns = required_data_columns - set(total_data)
    if missing_data_columns:
        missing = ", ".join(sorted(missing_data_columns))
        raise KeyError(f"total_data is missing plotting columns: {missing}")

    windows = experiment.set_index("spot_names")
    spot_windows = windows.reindex(total_data["spot_name"])
    time = pd.to_numeric(total_data["Time"], errors="coerce").to_numpy(dtype=float)

    def finite_bounds(start_column: str, end_column: str):
        start = pd.to_numeric(
            spot_windows[start_column], errors="coerce"
        ).to_numpy(dtype=float)
        end = pd.to_numeric(
            spot_windows[end_column], errors="coerce"
        ).to_numpy(dtype=float)
        return start, end

    baseline_start, baseline_end = finite_bounds(
        "baseline_start_times", "baseline_end_times"
    )
    plateau_start, plateau_end = finite_bounds(
        "plateau_start_times", "plateau_end_times"
    )
    baseline_mask = (
        np.isfinite(time)
        & np.isfinite(baseline_start)
        & np.isfinite(baseline_end)
        & (baseline_end > baseline_start)
        & (time >= baseline_start)
        & (time < baseline_end)
    )
    plateau_mask = (
        np.isfinite(time)
        & np.isfinite(plateau_start)
        & np.isfinite(plateau_end)
        & (plateau_end > plateau_start)
        & (time >= plateau_start)
        & (time < plateau_end)
    )
    selection_columns = [
        "baseline_start_times",
        "baseline_end_times",
        "plateau_start_times",
        "plateau_end_times",
    ]
    numeric_windows = experiment[selection_columns].apply(
        pd.to_numeric, errors="coerce"
    )
    incomplete_spots = int(numeric_windows.isna().any(axis=1).sum())
    title = "Original data with selected baseline and plateau"
    if incomplete_spots:
        noun = "spot" if incomplete_spots == 1 else "spots"
        title += f" ({incomplete_spots} {noun} missing selections)"

    return plot_series(
        [
            {"data": total_data, "y": channel, "color": color, "line_width": line_width, "label": channel},
            {"data": total_data.loc[baseline_mask], "y": channel, "color": "#c43d3d", "kind": "scatter", "size": 4, "label": "baseline"},
            {"data": total_data.loc[plateau_mask], "y": channel, "color": "#2b74a8", "kind": "scatter", "size": 4, "label": "plateau"},
        ],
        title=title,
        xlabel="Time (s)",
        ylabel="cps",
        width=width,
        height=height,
        max_display_points=max_display_points,
    )

def plot_time_series(
    dataframe,
    y=None,
    x="Time",
    color="black",
    kind="line",
    line_width=0.5,
    width=1200,
    height=400,
    max_display_points: int | None = None,
):
    """
    Plots time series data for multiple channels with individual curve coloring.

    Parameters:
        dataframe (pd.DataFrame): The DataFrame containing the data.
        y (str or list): The column name(s) of the channel(s) to plot.
        x (str): The column name for the x-axis (default is 'Time').
        color (str or list): The color(s) for the curves. If a list, it must match the length of `y`.
        line_width (float): The width of the lines (default is 0.5).
        width (int): The width of the plot (default is 1200).
        height (int): The height of the plot (default is 400).

    Returns:
        bokeh.plotting.figure: A native Bokeh figure.
    """
    if y is None:
        raise ValueError('Channel name(s) must be specified')

    # If y is a single channel, convert it to a list
    if isinstance(y, str):
        y = [y]
    elif not isinstance(y, list):
        raise ValueError('Channel name(s) must be a string or a list of strings')

    # If color is a single value, convert it to a list of the same length as y
    if isinstance(color, str):
        color = [color] * len(y)
    elif isinstance(color, list) and len(color) != len(y):
        raise ValueError('Length of `color` list must match the number of channels in `y`')

    return plot_series(
        [
            {
                "data": dataframe,
                "y": channel,
                "x": x,
                "color": channel_color,
                "kind": kind,
                "line_width": line_width,
                "label": channel,
            }
            for channel, channel_color in zip(y, color)
        ],
        x=x,
        title="Time Series Plot",
        xlabel=x,
        ylabel="cps",
        width=width,
        height=height,
        max_display_points=max_display_points,
    )


def plot_tw_spot_mean_session_projection(
    primary_standard_downhole_ratios,
    session_correction_results,
    standard_archive,
    standard: str | None = None,
    *,
    width: int = 900,
    height: int = 560,
):
    """Illustrate the spot-mean Tera-Wasserburg session correction.

    The plotted raw means are *session-uncorrected* primary-standard means;
    downhole correction has already been applied, matching the inputs used by
    :func:`UPbRPy.fractionation.calc_session_fractionation_TW`. The independent
    ``207Pb/206Pb`` session model is applied first. A short vertical dotted
    segment exposes that usually small Pb/Pb adjustment. The corrected
    ordinate is then held fixed while ``238U/206Pb`` is projected horizontally
    onto the reference-material mixing line. No uncertainty symbols are drawn
    so the geometry of the correction remains explicit.

    The named ``tw_session_spot_projection_source`` stores the plotted means,
    targets, and correction factors for auditing or tests.
    """
    from .calibration import evaluate_correction_result
    from .ratios import project_tw_238u_206pb

    required_columns = {
        "sample",
        "spot_name",
        "Time",
        "206Pb/238U_DH_corrected",
        "207Pb/206Pb_DH_corrected",
    }
    missing = required_columns - set(primary_standard_downhole_ratios)
    if missing:
        raise KeyError(
            "primary-standard ratio table is missing: "
            + ", ".join(sorted(missing))
        )
    if "207Pb/206Pb" not in session_correction_results:
        raise KeyError("session_correction_results lacks a 207Pb/206Pb model")

    available_standards = [
        str(value)
        for value in pd.unique(primary_standard_downhole_ratios["sample"].dropna())
    ]
    if standard is None:
        if len(available_standards) != 1:
            raise ValueError(
                "Pass standard when the ratio table contains multiple samples."
            )
        standard = available_standards[0]
    standard = str(standard)
    selected = primary_standard_downhole_ratios.loc[
        primary_standard_downhole_ratios["sample"].astype(str) == standard
    ].copy()
    if selected.empty:
        raise ValueError(f"No downhole-corrected ratios found for {standard!r}.")
    if standard not in standard_archive.index:
        raise KeyError(f"Reference material {standard!r} is not in the archive.")

    reference = standard_archive.loc[standard]
    radiogenic_68 = float(reference["206Pb/238U*"])
    radiogenic_76 = float(reference["207Pb/206Pb*"])
    initial_76 = float(reference["207Pb/206Pb_i"])
    radiogenic_86 = 1.0 / radiogenic_68

    pbpb_correction = evaluate_correction_result(
        session_correction_results["207Pb/206Pb"],
        pd.to_numeric(selected["Time"], errors="coerce").to_numpy(dtype=float),
    )
    selected["207Pb/206Pb_session_corrected"] = (
        pd.to_numeric(
            selected["207Pb/206Pb_DH_corrected"],
            errors="coerce",
        ).to_numpy(dtype=float)
        * pbpb_correction
    )

    records = []
    for spot_name, spot in selected.groupby("spot_name", sort=False):
        mean_68 = float(
            pd.to_numeric(
                spot["206Pb/238U_DH_corrected"],
                errors="coerce",
            ).mean()
        )
        mean_76_measured = float(
            pd.to_numeric(
                spot["207Pb/206Pb_DH_corrected"],
                errors="coerce",
            ).mean()
        )
        mean_76_corrected = float(
            pd.to_numeric(
                spot["207Pb/206Pb_session_corrected"],
                errors="coerce",
            ).mean()
        )
        mean_time = float(pd.to_numeric(spot["Time"], errors="coerce").mean())
        if not np.isfinite(
            [mean_68, mean_76_measured, mean_76_corrected, mean_time]
        ).all() or mean_68 <= 0:
            continue
        x_measured = 1.0 / mean_68
        x_target = project_tw_238u_206pb(
            mean_76_corrected,
            radiogenic_68,
            radiogenic_76,
            initial_76,
        )
        if not np.isfinite(x_target) or x_target <= 0:
            continue
        records.append(
            {
                "spot_name": str(spot_name),
                "Time": mean_time,
                "x_measured": x_measured,
                "y_measured": mean_76_measured,
                "y_pbpb_corrected": mean_76_corrected,
                "x_target": x_target,
                "pbpb_factor": mean_76_corrected / mean_76_measured,
                # The production model multiplies 206Pb/238U, so its factor
                # is target_68 / measured_68 = x_measured / x_target.
                "206Pb/238U_fractionation_factor": x_measured / x_target,
            }
        )
    projection = pd.DataFrame.from_records(records)
    if projection.empty:
        raise ValueError("No finite primary-standard spot means could be plotted.")

    source = ColumnDataSource(
        projection,
        name="tw_session_spot_projection_source",
    )
    plot = figure(
        width=int(width),
        height=int(height),
        title=(
            f"{standard}: spot-mean Tera-Wasserburg session projection"
        ),
        x_axis_label="238U/206Pb",
        y_axis_label="207Pb/206Pb",
        tools="pan,wheel_zoom,box_zoom,reset,save",
        active_scroll="wheel_zoom",
    )
    x_max = max(
        radiogenic_86,
        float(projection["x_measured"].max()),
        float(projection["x_target"].max()),
    ) * 1.05
    slope = (radiogenic_76 - initial_76) / radiogenic_86
    plot.line(
        [0.0, x_max],
        [initial_76, initial_76 + slope * x_max],
        color="black",
        line_width=2.2,
        legend_label="True reference isochron",
    )
    plot.scatter(
        [0.0],
        [initial_76],
        marker="square",
        size=10,
        color="black",
        legend_label="Initial/common-Pb endpoint",
    )
    plot.scatter(
        [radiogenic_86],
        [radiogenic_76],
        marker="star",
        size=14,
        color="#c62828",
        legend_label="Radiogenic endpoint",
    )

    raw_renderer = plot.scatter(
        x="x_measured",
        y="y_measured",
        source=source,
        marker="circle",
        size=9,
        color="#666666",
        alpha=0.8,
        legend_label="Session-uncorrected spot mean",
    )
    plot.segment(
        x0="x_measured",
        y0="y_measured",
        x1="x_measured",
        y1="y_pbpb_corrected",
        source=source,
        color="#2b74a8",
        line_dash="dotted",
        line_width=1.3,
        legend_label="Pb/Pb correction (usually small)",
    )
    corrected_start_renderer = plot.scatter(
        x="x_measured",
        y="y_pbpb_corrected",
        source=source,
        marker="diamond",
        size=8,
        color="#2b74a8",
        legend_label="Pb/Pb-corrected projection start",
    )
    plot.segment(
        x0="x_measured",
        y0="y_pbpb_corrected",
        x1="x_target",
        y1="y_pbpb_corrected",
        source=source,
        color="#e67e22",
        line_dash="dashed",
        line_width=1.5,
        legend_label="Horizontal U/Pb projection",
    )
    target_renderer = plot.scatter(
        x="x_target",
        y="y_pbpb_corrected",
        source=source,
        marker="circle",
        size=8,
        fill_alpha=0,
        line_color="#e67e22",
        line_width=1.7,
        legend_label="Projected true-ratio target",
    )
    plot.add_tools(
        HoverTool(
            renderers=[raw_renderer],
            tooltips=[
                ("spot", "@spot_name"),
                ("mean time", "@Time{0.0} s"),
                ("measured 238U/206Pb", "@x_measured{0.00000}"),
                ("measured 207Pb/206Pb", "@y_measured{0.00000}"),
            ],
        ),
        HoverTool(
            renderers=[corrected_start_renderer, target_renderer],
            tooltips=[
                ("spot", "@spot_name"),
                ("Pb/Pb-corrected 207Pb/206Pb", "@y_pbpb_corrected{0.00000}"),
                ("target 238U/206Pb", "@x_target{0.00000}"),
                ("206Pb/238U factor", "@{206Pb/238U_fractionation_factor}{0.00000}"),
                ("Pb/Pb factor", "@pbpb_factor{0.00000}"),
            ],
        ),
    )
    if plot.legend:
        legend = plot.legend[0]
        legend.location = "top_left"
        legend.click_policy = "hide"
        legend.label_text_font_size = "8pt"
        plot.add_layout(legend, "right")
    return plot


def plot_tw_standard_calibration(
    raw_ratios,
    downhole_corrected_ratios,
    fractionation_corrected_ratios,
    standard_archive,
    standard,
    *,
    width=620,
    height=520,
):
    """Show a Pb-rich standard's calibration in Tera-Wasserburg space.

    The true mixing line joins the standard's radiogenic endpoint to its
    initial/common-Pb composition. Raw, downhole-corrected, and fully corrected
    measurements are overlaid. Dashed horizontal segments show the
    session-stage ``238U/206Pb`` projection at the fully Pb/Pb-corrected
    ``207Pb/206Pb`` value; hollow points mark the corresponding targets on the
    true line. Separate panels show every sweep and the arithmetic spot means.

    Parameters
    ----------
    raw_ratios, downhole_corrected_ratios, fractionation_corrected_ratios
        Sweep-wise tables returned by :func:`calc_raw_ratios`,
        :func:`apply_DH_correction`, and :func:`apply_session_correction`.
    standard_archive : pandas.DataFrame
        Reference-material archive containing the standard's radiogenic
        ``206Pb/238U`` and ``207Pb/206Pb`` plus initial ``207Pb/206Pb``.
    standard : str
        Primary Pb-rich reference-material name.

    Returns
    -------
    dict
        ``layout``, ``sweepwise``, and ``spot_mean`` Bokeh objects plus the two
        projection DataFrames used to draw the horizontal corrections.
    """
    from .ratios import project_tw_238u_206pb

    reference = standard_archive.loc[standard]
    radiogenic_68 = float(reference["206Pb/238U*"])
    radiogenic_76 = float(reference["207Pb/206Pb*"])
    initial_76 = float(reference["207Pb/206Pb_i"])
    radiogenic_86 = 1.0 / radiogenic_68

    required = {
        "raw": {"sample", "spot_name", "Time", "206Pb/238U", "207Pb/206Pb"},
        "downhole": {
            "sample",
            "spot_name",
            "Time",
            "206Pb/238U_DH_corrected",
            "207Pb/206Pb_DH_corrected",
        },
        "corrected": {
            "sample",
            "spot_name",
            "Time",
            "206Pb/238U_fractionation_corrected",
            "207Pb/206Pb_fractionation_corrected",
        },
    }
    tables = {
        "raw": raw_ratios,
        "downhole": downhole_corrected_ratios,
        "corrected": fractionation_corrected_ratios,
    }
    for name, columns in required.items():
        missing = columns - set(tables[name])
        if missing:
            raise KeyError(
                f"{name} ratio table is missing: " + ", ".join(sorted(missing))
            )

    def selected(frame):
        return frame.loc[frame["sample"].astype(str) == str(standard)].copy()

    raw = selected(raw_ratios)
    downhole = selected(downhole_corrected_ratios)
    corrected = selected(fractionation_corrected_ratios)
    if raw.empty or downhole.empty or corrected.empty:
        raise ValueError(f"No complete calibration stages found for {standard!r}.")

    def coordinates(frame, ratio_68, ratio_76):
        result = frame[["spot_name", "Time"]].copy()
        values_68 = pd.to_numeric(frame[ratio_68], errors="coerce").to_numpy()
        result["x"] = np.divide(
            1.0,
            values_68,
            out=np.full(values_68.shape, np.nan, dtype=float),
            where=np.isfinite(values_68) & (values_68 != 0),
        )
        result["y"] = pd.to_numeric(frame[ratio_76], errors="coerce").to_numpy()
        return result.loc[np.isfinite(result["x"]) & np.isfinite(result["y"])]

    sweep_stages = {
        "Raw": coordinates(raw, "206Pb/238U", "207Pb/206Pb"),
        "Downhole corrected": coordinates(
            downhole,
            "206Pb/238U_DH_corrected",
            "207Pb/206Pb_DH_corrected",
        ),
        "Fully corrected": coordinates(
            corrected,
            "206Pb/238U_fractionation_corrected",
            "207Pb/206Pb_fractionation_corrected",
        ),
    }

    join_keys = ["spot_name", "Time"]
    sweep_projection = downhole[
        [*join_keys, "206Pb/238U_DH_corrected"]
    ].merge(
        corrected[
            [
                *join_keys,
                "206Pb/238U_fractionation_corrected",
                "207Pb/206Pb_fractionation_corrected",
            ]
        ],
        on=join_keys,
        how="inner",
    )
    sweep_projection["x_before_horizontal"] = 1.0 / sweep_projection[
        "206Pb/238U_DH_corrected"
    ]
    sweep_projection["x_corrected"] = 1.0 / sweep_projection[
        "206Pb/238U_fractionation_corrected"
    ]
    sweep_projection["y_corrected"] = sweep_projection[
        "207Pb/206Pb_fractionation_corrected"
    ]
    sweep_projection["x_target"] = project_tw_238u_206pb(
        sweep_projection["y_corrected"],
        radiogenic_68,
        radiogenic_76,
        initial_76,
    )
    finite_projection = np.isfinite(
        sweep_projection[
            ["x_before_horizontal", "x_corrected", "y_corrected", "x_target"]
        ]
    ).all(axis=1)
    sweep_projection = sweep_projection.loc[finite_projection].copy()

    def mean_coordinates(frame, ratio_68, ratio_76):
        means = frame.groupby("spot_name", sort=False).agg(
            Time=("Time", "mean"),
            ratio_68=(ratio_68, "mean"),
            y=(ratio_76, "mean"),
        ).reset_index()
        means["x"] = 1.0 / means["ratio_68"]
        return means[["spot_name", "Time", "x", "y"]]

    spot_stages = {
        "Raw": mean_coordinates(raw, "206Pb/238U", "207Pb/206Pb"),
        "Downhole corrected": mean_coordinates(
            downhole,
            "206Pb/238U_DH_corrected",
            "207Pb/206Pb_DH_corrected",
        ),
        "Fully corrected": mean_coordinates(
            corrected,
            "206Pb/238U_fractionation_corrected",
            "207Pb/206Pb_fractionation_corrected",
        ),
    }
    spot_projection = sweep_projection.groupby("spot_name", sort=False).agg(
        Time=("Time", "mean"),
        mean_68_downhole=("206Pb/238U_DH_corrected", "mean"),
        mean_68_corrected=("206Pb/238U_fractionation_corrected", "mean"),
        y_corrected=("y_corrected", "mean"),
    ).reset_index()
    spot_projection["x_before_horizontal"] = 1.0 / spot_projection[
        "mean_68_downhole"
    ]
    spot_projection["x_corrected"] = 1.0 / spot_projection[
        "mean_68_corrected"
    ]
    spot_projection["x_target"] = project_tw_238u_206pb(
        spot_projection["y_corrected"],
        radiogenic_68,
        radiogenic_76,
        initial_76,
    )

    stage_styles = {
        "Raw": ("#777777", "circle"),
        "Downhole corrected": ("#2b74a8", "triangle"),
        "Fully corrected": ("#00b050", "diamond"),
    }

    def calibration_plot(stage_frames, projection, title, marker_size):
        plot = figure(
            width=width,
            height=height,
            title=title,
            x_axis_label="238U/206Pb",
            y_axis_label="207Pb/206Pb",
            tools="pan,wheel_zoom,box_zoom,reset,save",
            active_scroll="wheel_zoom",
        )

        finite_x = [radiogenic_86]
        for frame in stage_frames.values():
            finite_x.extend(frame.loc[np.isfinite(frame["x"]), "x"].tolist())
        finite_x.extend(projection.get("x_target", pd.Series(dtype=float)).tolist())
        x_max = max(finite_x) * 1.05
        slope = (radiogenic_76 - initial_76) / radiogenic_86
        plot.line(
            [0.0, x_max],
            [initial_76, initial_76 + slope * x_max],
            color="black",
            line_width=2,
            legend_label="True reference isochron",
        )
        plot.scatter(
            [0.0],
            [initial_76],
            marker="square",
            size=10,
            color="black",
            legend_label="Initial/common-Pb endpoint",
        )
        plot.scatter(
            [radiogenic_86],
            [radiogenic_76],
            marker="star",
            size=13,
            color="#c62828",
            legend_label="Radiogenic endpoint",
        )

        projection_source = ColumnDataSource(projection)
        plot.segment(
            x0="x_before_horizontal",
            y0="y_corrected",
            x1="x_target",
            y1="y_corrected",
            source=projection_source,
            color="#e67e22",
            alpha=0.35,
            line_dash="dashed",
            line_width=1,
            legend_label="Horizontal projection to true isochron",
        )
        target_renderer = plot.scatter(
            x="x_target",
            y="y_corrected",
            source=projection_source,
            marker="circle",
            size=max(marker_size - 1, 4),
            fill_alpha=0,
            line_color="#e67e22",
            line_width=1.5,
            legend_label="Projected target",
        )
        plot.add_tools(
            HoverTool(
                renderers=[target_renderer],
                tooltips=[
                    ("spot", "@spot_name"),
                    ("target 238U/206Pb", "@x_target{0.00000}"),
                    ("corrected 207Pb/206Pb", "@y_corrected{0.00000}"),
                ],
            )
        )

        for label, frame in stage_frames.items():
            color, marker = stage_styles[label]
            source = ColumnDataSource(frame)
            renderer = plot.scatter(
                x="x",
                y="y",
                source=source,
                color=color,
                marker=marker,
                size=marker_size,
                alpha=0.65,
                legend_label=label,
            )
            plot.add_tools(
                HoverTool(
                    renderers=[renderer],
                    tooltips=[
                        ("stage", label),
                        ("spot", "@spot_name"),
                        ("238U/206Pb", "@x{0.00000}"),
                        ("207Pb/206Pb", "@y{0.00000}"),
                    ],
                )
            )
        plot.legend.location = "top_right"
        plot.legend.click_policy = "hide"
        plot.legend.label_text_font_size = "8pt"
        return plot

    sweep_plot = calibration_plot(
        sweep_stages,
        sweep_projection,
        f"{standard}: sweep-wise Tera-Wasserburg calibration",
        marker_size=5,
    )
    spot_plot = calibration_plot(
        spot_stages,
        spot_projection,
        f"{standard}: spot-mean Tera-Wasserburg calibration",
        marker_size=9,
    )
    return {
        "layout": bokeh_row(sweep_plot, spot_plot),
        "sweepwise": sweep_plot,
        "spot_mean": spot_plot,
        "sweepwise_projection": sweep_projection,
        "spot_mean_projection": spot_projection,
    }

__all__ = [
    'StandaloneBokehPlot',
    'add_time_series',
    'depth_profile_concordia_editor',
    'create_vector_export_webdriver',
    'excel_plateau_inlier_mask',
    'export_bokeh_plots',
    'export_bokeh_vector',
    'find_single_bokeh_plot',
    'inline_correction_plots',
    'inline_plot_grid',
    'inline_plot_pair',
    'initialize_bokeh',
    'initialize_holoviews',
    'initialize_notebook',
    'initialize_panel_extension',
    'selection_editor',
    'plot_series',
    'plot_selected_data',
    'plot_time_series',
    'plot_tw_spot_mean_session_projection',
    'plot_tw_standard_calibration',
    'standalone_bokeh_resources',
    'extrema_preserving_display_sample',
]
