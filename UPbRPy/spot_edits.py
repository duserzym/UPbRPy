"""Acquisition identity and replay of reviewed session edits, independent of Qt."""

from copy import deepcopy


EDIT_KEYS = ("spot_renames", "sample_overrides", "removed_spots", "calculation_excluded_spots")


def original_spot_label(experiment, spot_name):
    """Return the raw acquisition label of a possibly renamed spot."""
    return experiment.attrs.get("spot_renames", {}).get(spot_name, spot_name)


def spot_edit_metadata(experiment):
    """Return the JSON-safe decisions needed to replay session edits."""
    return {key: deepcopy(experiment.attrs[key]) for key in EDIT_KEYS if key in experiment.attrs}


def restore_spot_edits(experiment, total_data, edits):
    """Replay saved removals, renames and materials before selection matching.

    Validate all original labels and final identities before returning new
    frames. Raw acquisition objects and source files are never edited.
    """
    edits = deepcopy(edits)
    renames = edits.get("spot_renames", {})
    removed = edits.get("removed_spots", [])
    imported = set(experiment["spot_names"].astype(str))
    missing = (set(renames.values()) | set(removed)) - imported
    if missing:
        raise ValueError("Edited acquisitions missing from raw data: " + ", ".join(sorted(missing)))
    if len(set(renames.values())) != len(renames):
        raise ValueError("Two renamed spots refer to the same acquisition.")
    mapping = {old: new for new, old in renames.items()}
    experiment = experiment.loc[~experiment["spot_names"].isin(removed)].copy(deep=True)
    total_data = total_data.loc[~total_data["spot_name"].isin(removed)].copy(deep=True)
    experiment["spot_names"] = experiment["spot_names"].replace(mapping)
    total_data["spot_name"] = total_data["spot_name"].replace(mapping)
    names = experiment["spot_names"].astype(str)
    if names.str.casefold().duplicated().any() or names.str.strip().eq("").any():
        raise ValueError("Saved spot names are empty or clash with another acquisition.")
    # Manifest corrections are re-imported under their original keys.
    overrides = {mapping.get(key, key): value for key, value in
                 experiment.attrs.get("sample_overrides", {}).items()
                 if key not in removed}
    if "sample_overrides" in edits:
        from .io import sample_from_label

        overrides = edits["sample_overrides"]
        rule = experiment.attrs.get("sample_label_rule", {})
        # An explicitly empty saved map can mean the viewer reverted a
        # manifest correction. Start from raw-label samples before replaying
        # the complete saved map, rather than retaining that import override.
        derived = {name: sample_from_label(renames.get(name, name), rule.get("sep", "_"),
                                          rule.get("aliases")) for name in names}
        experiment["sample"] = names.map(derived)
        total_data["sample"] = total_data["spot_name"].map(derived)
    for name, override in overrides.items():
        if name not in set(names):
            raise ValueError(f"Sample override refers to an unknown spot: {name}")
        experiment.loc[experiment["spot_names"].eq(name), "sample"] = override["sample"]
        total_data.loc[total_data["spot_name"].eq(name), "sample"] = override["sample"]
    experiment.attrs.update(edits)
    experiment.attrs["sample_overrides"] = overrides
    if "calculation_excluded_spots" in edits:
        experiment.attrs["calculation_excluded_spots"] = sorted(
            set(edits["calculation_excluded_spots"]) & set(names))
    return experiment, total_data
