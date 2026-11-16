# Open a session

1. Export a **Rich single CSV** from Qtegra. Retain the per-spot folder when
   available: it supplies recorded dwell times, cross-calibration factors and
   instrument metadata. See [Choosing an export](../guide/choosing-an-export.md).
2. In Studio choose **Import Rich CSV…** or **File > Open Qtegra file…**.
   A folder can also be opened directly. Accept the companion-folder offer only
   when it corresponds to that Rich export.
3. Confirm the mineral preset, spot groups, channel list and source-format
   badge. Read its capability and warning tooltip.
4. Review the [auto-picked windows](../manual/selections.md), then run each
   workflow action in order. Save a `.upbr` project with Command/Ctrl+S.
5. Check secondary references and final flags before exporting results.

Drag a supported source into Studio or use Open Recent to return to it. Open
Project restores an existing reduction. New content opens in another tab,
reusing the active tab when it is empty and idle. Closing the last tab leaves
an empty tab. Use the Window menu to move a tab to a separate window.

For a portable input package use a session bundle with raw exports and a
manifest. [Database sessions](../manual/database.md) arrive this way as well.
A Rich file with Y and either Time or StartTime is accepted; missing timing or
metadata capabilities produce explicit warnings. Y-only UPbR files are rejected.
