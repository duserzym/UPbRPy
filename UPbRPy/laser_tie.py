"""Tie Chromium scans to Qtegra acquisitions without interpreting local zones.

Naive laser and acquisition datetimes are treated as UTC wall clocks. Aware
acquisition starts are converted to UTC. The fitted offset absorbs the zone
and clock difference; neither reader's original timestamps are changed.
"""

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import timezone
from math import isfinite
from statistics import median

from .laserlog import LaserLog, read_lase


@dataclass(frozen=True)
class LaserTieRow:
    """One acquisition's match and acquisition-relative firing window."""

    status: str = "unlinked"
    scan_position: int | None = None
    description_number: int | None = None
    laser_on: float | None = None
    laser_off: float | None = None
    duration: float | None = None
    fluence_j_cm2: float | None = None
    rep_rate_hz: float | None = None
    spot_um: float | None = None
    spot_shape: str | None = None
    shot_count: int | None = None
    output_pct: float | None = None
    stage_um: tuple[float, float, float] | None = None


@dataclass(frozen=True)
class LaserTie:
    """A portable tie; stage coordinates are retained here for display only."""

    sha256: str
    file_name: str
    system_type: str
    laser_instrument: str
    spot_key: str
    threshold_s: float
    has_laser_times: bool
    clock_offset_s: float | None
    clock_offset_spread_s: float | None
    pairs: int
    counts: dict[str, int]
    warnings: tuple[str, ...]
    rows: dict[str, LaserTieRow]
    requires_confirmation: bool = False

    def to_dict(self) -> dict:
        """Return JSON-safe values, including the complete per-label tie."""
        result = asdict(self)
        result["warnings"] = list(self.warnings)
        for row in result["rows"].values():
            if row["stage_um"] is not None:
                row["stage_um"] = list(row["stage_um"])
        return result

    @classmethod
    def from_dict(cls, value: dict) -> "LaserTie":
        """Restore a tie saved by to_dict, without needing the source file."""
        data = dict(value)
        data["warnings"] = tuple(data["warnings"])
        data["rows"] = {
            label: LaserTieRow(**{**row, "stage_um": tuple(row["stage_um"])
                                 if row["stage_um"] is not None else None})
            for label, row in data["rows"].items()
        }
        return cls(**data)

    def windows(self) -> dict[str, tuple[float | None, float | None]]:
        """Return linked windows for laser_guidance.add_laser_timing."""
        return {label: (row.laser_on, row.laser_off)
                for label, row in self.rows.items() if row.status == "linked"}

    def apply(self, experiment, *, confirmed: bool = False) -> None:
        """Add timing only, requiring confirmation for settings-only logs."""
        from .laser_guidance import add_laser_timing

        if self.requires_confirmation and not confirmed:
            raise ValueError("Confirm the settings-only laser tie before applying it.")
        renames = experiment.attrs.get("spot_renames", {})
        windows = self.windows()
        add_laser_timing(
            experiment, {label: windows[renames.get(label, label)]
                         for label in experiment.spot_names if renames.get(label, label) in windows},
            dict(zip(experiment.spot_names, experiment.spot_starting_times)),
            scan_positions={label: self.rows[renames.get(label, label)].scan_position
                            for label in experiment.spot_names if renames.get(label, label) in windows},
            source="lase", log_sha256=(self.sha256,), spot_key=self.spot_key,
        )


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def tie_laser_log(log: LaserLog, acquisitions, *, spot_key="position", threshold_s=30.0) -> LaserTie:
    """Match acquisition spot numbers to scan positions or description numbers.

    Accept a QtegraSession or an iterable of Qtegra acquisitions. Rich metadata
    has already replaced the label-derived spot_number when available. Like
    UPbDB, only unique candidates estimate the clock; repeated firings (Spot or
    Line) use the available firing nearest that clock, once each. Ambiguous
    candidates without a clock are doubtful. No time fallback is attempted:
    absent number matches remain unlinked in this standalone workflow.
    """
    threshold_s = float(threshold_s)
    if spot_key not in {"position", "description"} or not isfinite(threshold_s) or not 1 <= threshold_s <= 600:
        raise ValueError("Choose position or description and a threshold from 1 to 600 seconds.")
    acquisitions = getattr(acquisitions, "acquisitions", acquisitions)
    # The experiment reader also retains only the first repeated label.
    unique = {}
    for acquisition in acquisitions:
        unique.setdefault(acquisition.label, acquisition)
    acquisitions = list(unique.values())
    timed = log.first_laser_on is not None
    keyed = defaultdict(list)
    for scan in log.scans:
        key = scan.index + 1 if spot_key == "position" else scan.spot_number
        if key is not None:
            firings = [f for f in scan.firings if f.laser_on is not None] if timed else [None]
            keyed[key].extend((scan, firing) for firing in firings)
    choices = {a.label: keyed.get(a.spot_number, []) if not timed or a.start is not None else []
               for a in acquisitions}

    def offset(a, candidate):
        return (_utc(a.start) - _utc(candidate[1].laser_on)).total_seconds()

    offsets = [offset(a, choices[a.label][0]) for a in acquisitions
               if timed and len(choices[a.label]) == 1]
    center = float(median(offsets)) if offsets else None
    spread = float(median(abs(v - center) for v in offsets)) if offsets else None
    warnings = [w.message for w in log.warnings]
    if not timed:
        warnings.append("This log has no laser times; matched settings by number only. Confirm before applying.")
    elif len(offsets) < 3:
        warnings.append("Too few matched spots to check the clock (fewer than 3 pairs).")
    used = set()
    rows = {}
    for a in acquisitions:
        opts = [(s, f) for s, f in choices[a.label]
                if (s.index, f.firing_index if f else None, f.pass_index if f else None) not in used]
        if not opts:
            rows[a.label] = LaserTieRow()
            continue
        if timed and center is None:
            rows[a.label] = LaserTieRow(status="doubtful")
            continue
        if not timed and len(opts) != 1:
            rows[a.label] = LaserTieRow()
            continue
        scan, firing = min(opts, key=lambda c: (abs(offset(a, c) - center), c[0].index,
                                                c[1].firing_index, c[1].pass_index)) if timed else opts[0]
        status = "linked" if not timed or abs(offset(a, (scan, firing)) - center) <= threshold_s else "doubtful"
        if status == "linked":
            used.add((scan.index, firing.firing_index if firing else None, firing.pass_index if firing else None))
        on = center - offset(a, (scan, firing)) if timed else None
        off = ((_utc(firing.laser_off) - _utc(a.start)).total_seconds() + center
               if firing and firing.laser_off is not None else None)
        settings = scan.ablation
        rows[a.label] = LaserTieRow(
            status=status, scan_position=scan.index + 1, description_number=scan.spot_number,
            laser_on=on, laser_off=off, duration=firing.laser_duration_s if firing else None,
            fluence_j_cm2=settings.fluence_j_cm2, rep_rate_hz=settings.rep_rate_hz,
            spot_um=settings.spot_size_um, spot_shape=settings.spot_shape,
            shot_count=settings.shot_count, output_pct=settings.output_pct,
            stage_um=(firing.start_position_um if firing and firing.start_position_um is not None
                      else scan.vertices_um[0] if scan.vertices_um else None),
        )
    counts = Counter(row.status for row in rows.values())
    return LaserTie(
        sha256=log.sha256, file_name=log.path.name if log.path else "",
        system_type=log.system_info.get("SystemType", ""), laser_instrument="",
        spot_key=spot_key, threshold_s=threshold_s, has_laser_times=timed,
        clock_offset_s=center, clock_offset_spread_s=spread, pairs=len(offsets),
        counts={key: counts[key] for key in ("linked", "doubtful", "unlinked")},
        warnings=tuple(warnings), rows=rows, requires_confirmation=not timed,
    )


def read_session_with_laser(source, lase_path, *, spot_key="position", threshold_s=30.0,
                            confirm_settings_only=False, **read_options):
    """Return (tie, selected experiment) for a QtegraSession or session path.

    Forward read_options to read_experiment_data (for example primary_channel).
    Settings-only ties require confirm_settings_only=True. No source is written.
    """
    from .io import read_experiment_data
    from .qtegra import QtegraSession, open_qtegra
    from .session_manifest import raw_directory_for

    session = source if isinstance(source, QtegraSession) else open_qtegra(
        raw_directory_for(source), cycle_time_s=read_options.get("cycle_time_s"),
    )
    tie = tie_laser_log(read_lase(lase_path), session, spot_key=spot_key, threshold_s=threshold_s)
    experiment, _, _ = read_experiment_data(
        session, laser_tie=tie, laser_tie_confirmed=confirm_settings_only, **read_options,
    )
    return tie, experiment


__all__ = ["LaserTie", "LaserTieRow", "tie_laser_log", "read_session_with_laser"]
