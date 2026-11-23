# Spot concordia

![Spot concordia in UPbRPy Studio](../assets/screenshots/09-spot-concordia.png)

## What this view does

Displays corrected spot ratios as points and uncertainty ellipses on concordia.
The status line reports weighted-mean 206Pb/238U, 207Pb/235U and 207Pb/206Pb
dates for the selected shown spots, or all shown spots when nothing is selected.
Hidden spots are excluded from these dates. Each date uses its finite values
and positive uncertainties; the tooltip and **details** show its n and MSWD.
The engine's `weighted_mean_and_mswd` accepts absolute 2σ uncertainties and
reports an internal 2σ uncertainty without MSWD inflation. The uncertainty
branch follows the plot's calibration uncertainty setting. Studio does not
calculate a concordia age.

## Controls

Select **Spot concordia** under Final. The toolbar holds the diagram, colouring,
mouse mode, zoom axes, Grid, uncertainty level and Reset. Controls that do not
fit are available in **⋯**. **Sample** colours are the default; **Spot** and
**Single** are alternatives. Age-label spacing and precision adapt to zoom.
Hover reveals spot details, ratios and dates.

Click a sample folder's colour dot in the Spots tree, or right-click the folder
and choose **Set sample colour…**. The compact picker has palette swatches,
**Custom colour…**, and **Reset to default**. For individual spots, right-click
and choose **Set colour…** or **Reset to sample colour**. Command/Ctrl-click or
Shift-click selects several tree rows; a colour action applies to the selected
spots when the clicked row belongs to that selection. Spot overrides appear as
colour dots beside the rows, alongside any review-flag icon or material badge.
Custom display folders do not change a spot's sample/material colour identity.

In the concordia legend, **right-click the sample swatch** to open the same
sample picker. Left-click the label or swatch to hide/show the entire sample,
including its ellipses. A label click also clears the point selection. Hidden
samples stay hidden after a colour change.

In **Sample** mode a spot override wins over the assigned sample colour. In
**Spot** mode it wins over the spot palette. **Single** uses the theme accent
and ignores all overrides. **Colour → Reset all colours** clears both sample
colours and spot overrides. Every colour action, including a multi-spot change
or reset, is one selection undo/redo step. Colours are saved in the project;
renames carry spot overrides, and removing a spot drops its override with undo.
Colour edits repaint existing results and fits without recalculating reductions.

Assigned colours also appear in the bias graph and reference-material diagnostic
points/traces. Calibration fit lines and unassigned calibration points retain
isotope colours; the session overview retains its channel colour. Depth points
retain their time colour map, while depth sample chips and sample fits share the
assigned sample colours.

**Samples** opens sample chips, All, None and Glasses. Click a chip to show or
hide that sample; a partial sample becomes fully shown. Option-click on macOS
(Alt-click elsewhere) shows only that sample. Shift-click adds a range in
acquisition order. Command/Ctrl-click also toggles. Space or Return toggles a
focused chip. These choices clear the spot selection. Studio's inspector Spots
tree remains the chooser; standalone reviews have a **Spots** popover.

**Select** is the default on both concordia pages. Drag selects a rectangle;
Shift adds and Command/Ctrl toggles, including Command/Ctrl+Shift. Click selects
an acquisition, Shift-click extends and Command/Ctrl-click toggles. Pan and Zoom
retain their drag actions, with Shift+drag selecting in either mode. Space+drag
or middle-button drag pans in every mode. Trackpad scrolling pans; a mouse-wheel
notch, Command/Ctrl+scroll or a pinch zooms about the cursor or fingers, respecting
XY/X/Y. Double-click resets the view. The **?** tooltip lists these gestures.

**H** or **E** toggles calculation hiding; **Shift+H** shows all and **Escape**
clears selection. Right-click offers Hide/Unhide and visibility actions. Hidden
spots remain hollow with muted dashed outlines and are omitted from weighted
dates, fits and publication weighted means. Calculation exclusions are saved in
the project. Plot visibility choices do not change publication exports.

Selecting one spot opens its date card inside the plot. **×** hides the card
until the selection changes. **Focus**, or **View > Focus on the plot**
(Command/Ctrl+Shift+F), folds the workflow rail and inspector to handles and
removes the stage row. A handle unfolds its panel alone. Turning Focus off
restores the previous widths. This viewer preference is separate from project
data and from hiding a panel through View. Session-link controls are available
in the concordia toolbar's **Session** popover.

**Fit** opens the on/off toggle and options: models 1–3, common-Pb and age
anchors, decay-constant uncertainty, a combined fit, bootstrap resamples and
seed. New sessions start with Fit off for every mineral. Each concordia page
keeps its own on/off state, which only the toggle (or the right-click **Fit selected**
action) changes; it is saved in the project. While Fit is off no fit line is
drawn, whatever you select, hide, exclude, zoom or undo; the status line keeps
the last results, marked "Fit off". While Fit is on, the fit recomputes when
its spots change. Fits use selected shown points, or all shown points if none
are selected, after calculation exclusions. If every selected spot is hidden
or excluded, there is no fit and the status reads "No spots to fit"; clearing
the selection fits all shown spots again. Analytical results appear first, followed by
progressive bootstrap results. Defaults are 2000 resamples and seed 1; long
runs use 500-resample chunks with seed + chunk index. The settings and last
results are saved in the project. The status line summarises intercepts, MSWD
and bootstrap progress; **details** opens the complete readout.

**Export fits…** writes fit tables, footnotes, SVG and PNG. **Export results…**
includes the last fits from both concordia pages in its audit bundle.

Depth concordia uses the same layout, with a focused acquisition, Glasses,
diagram, colour map and selection actions in its toolbar and popovers.

## What to check

Inspect n of N, exclusions, 2σ uncertainties, bootstrap intervals and fit
residuals. A large MSWD warrants checking uncertainties and geological
assumptions. See [regression methods](../isochron_regression.md) for the numerical
models and validation. A visually close fit alone does not establish a valid age.
