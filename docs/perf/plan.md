# Studio performance plan

The menu, channel defaults, database submit controls and crash guard from the
architecture chat are already implemented. The Zhang2 reviewed project and
Zhang1 automatic project were restored with Studio's project loader, reduced
and exported through the shared engine, then imported into the local database
and marked preferred. All 324 Zhang2 saved windows were restored exactly;
original project files remained byte-identical. Both imports matched their
raw input hash, with no quarantine or unknown metrics.

The full takeover audit passed 1,110 tests with 16 existing skips using the
repository-prescribed offscreen command and blocked network/keyring imports.

## Remaining performance work

1. Replace the full all-string Rich CSV frame with bounded or typed parsing.
   Preserve exact numeric results, empty-cell masks, recorded timestamps,
   detector data, native experiment dtypes and error messages. Use the existing
   Qtegra fixture suite unchanged and compare a real session independently.
2. Evaluate views into per-session quantity cubes instead of per-acquisition
   copies, with explicit mutation ownership and release tests.
3. Profile actual GUI startup imports before moving optional plotting and
   export dependencies to their use sites. The package facade is already lazy;
   changing it alone will not address transitive imports.
4. Audit closed-tab references, signal callbacks and plot items. Add a weakref
   collection regression before claiming that closing a tab releases its
   session.
5. Profile trace downsampling and idle timers with real channel counts.

The earlier Studio baseline was 304–309 MB idle, 589–636 MB with Zhang2 and
705–762 MB with Zhang1 in a second tab. The Rich parser high-water allocation
was a major contributor. These are queued changes, with no claimed memory
improvement in UPbRPy from this takeover.

## Current GUI measurement, 2026-11-06

Fresh macOS physical-footprint samples on the current source measured
299 to 303 MiB with no project, 656 MiB with the saved Zhang2 project restored,
and 645 MiB with both saved Zhang2 and Zhang1 projects open, unchanged after
20 seconds. The projects were separate exported copies opened through
`StudioWindowController` and the normal project loader with temporary settings.
The original projects were unchanged. These are short physical-footprint
samples, affected by macOS compression and the restored workflow stage; they
do not prove a reduction from the earlier full-workflow baseline above. The
parser and tab-lifetime optimizations remain pending. The workflow shell is
now named UPb Lab; this reduction application's name stays UPbRPy Studio.
