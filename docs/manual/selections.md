# Review baseline and plateau picks

![Review baseline and plateau picks in UPbRPy Studio](../assets/screenshots/02-review-baseline-and-plateau-picks.png)

## What this step does

Provides the manual quality check before reduction. Baseline sweeps estimate
the gas blank. Plateau sweeps determine spot means; the wider signal interval
also supplies depth profiles. The falling washout edge is detected separately.

## Controls

Select a spot in **Browse** or use the previous/next spot shortcuts. Drag the
baseline or plateau handles, or the signal-start and signal-end markers. Use
**B** or **P** to select the active window; choose **Edit Signal Window**
in the Selection Windows menu to nudge signal edges; **Alt+Left/Right** moves its start
one measured sweep, and **Alt+Shift+Left/Right** moves its end. Window changes
are applied after a short debounce and already completed stages recalculate in
the background. Moving to another spot or tab does not discard an edit.

**Reset current spot** re-picks one acquisition. **Re-pick all spots** applies
the current parameters as one undoable edit; the Selection Windows menu also
has current-sample and all-open-tab actions. Manually edited spots are kept
unless **Also overwrite manual edits** is selected. Undo and redo cover drags,
nudges and batch picks.

**Nominal on-peak** limits on-peak duration from the detected onset and snaps
to a measured sweep. The signal handle can extend one sweep farther for the
grab gap. **Auto** leaves the detected washout edge in control. The
median and 10–90% range beneath the control help compare picks across the session.
The optional iterative plateau filter marks retained points filled and rejected
points hollow; mean bars and uncertainty rectangles update with the windows.

Save a project with **Ctrl+S** (Command on macOS). Once a project has a file,
selection edits save automatically. Until the first save, Studio keeps a recovery
copy; reopening the same source offers restoration.

## What to check

Keep the baseline before the laser onset, away from spikes and washout. Keep the
plateau within useful signal, avoiding inclusions, unstable tails and transitions.
Check low-count Pb as well as the matrix channel. Do not accept a pick solely
because it was made automatically. The screenshot shows baseline, plateau and signal
edge edits on a reference acquisition.

The four sweep fields in **Browse**, beside the selection colour controls,
edit Signal begin, On-peak begin, On-peak end and Signal end. Sweep numbers
start at 1; tooltips give their session time. On-peak end is an exclusive
boundary. Enter or Tab commits, Esc restores the displayed sweep, and arrows
or spin buttons step one sweep. These fields follow the same workflow locks
as the plot.

Dragging or typing an on-peak edge pushes the signal outward when needed.
Moving signal inward pushes on-peak inward, stopping when the on-peak window
has three retained sweeps (or the available interval in a shorter spot).
Moving the whole on-peak box preserves its width and pushes signal edges.
All edges snap to measured sweeps in this acquisition. The grabbed and pushed
edges brighten during movement, and a live label gives the edge, sweep and
time. Statistics refresh on release, and one undo restores every pushed edge.

Import and re-pick leave at least one measured sweep between each on-peak
handle and its signal handle where available. In zero-based indices, on-peak
`[p, q)` and inclusive signal `[s, e]` use `s <= p - 1` and `e >= q + 1`,
clamped to the first and last sweeps. Saved and manually reviewed windows
keep their recorded values.
