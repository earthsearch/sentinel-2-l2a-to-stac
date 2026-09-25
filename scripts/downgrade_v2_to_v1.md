# v2 → v1 downgrade: integration notes

Plan for folding `downgrade_v2_to_v1.py` (a POC, not yet wired in) into the
task as an **optional output mode**. The default v2 (STAC 1.1) output stays
unchanged.

## What the converter does

A dict → dict transform in four independent steps. Each one can be replaced
on its own:

1. **Version + extensions**: sets `stac_version` to `1.0.0` and maps these
   extension schemas back to their v1 versions: eo/projection/raster
   `v2.0.0` → `v1.1.0`, storage `v2.0.0` → `v1.0.0`.
2. **Projection**: `proj:code: "EPSG:n"` becomes `proj:epsg: n`. Raises on a
   code that isn't EPSG.
3. **Storage**: removes asset `storage:refs` and collapses
   `storage:schemes` into item-level `storage:platform` / `region` /
   `requester_pays`. Raises if the schemes disagree.
4. **Bands**: copies asset-level band fields into each band, then splits
   them into `eo:bands` / `raster:bands`. The key mappings come from
   inverting `EO_BAND_RENAME` / `RASTER_BAND_RENAME` in `constants.py`.
   Raises on a band field with no v1 mapping.

Deliberately not restored: the `via` link (removed upstream on purpose),
`s2:dark_features_percentage` (absent from the source data), 
Delibrately not changed: the task-identity fields (`processing:software` key, 
payload id, hrefs), and key order.

## Integration

- **Flag**: an optional boolean, default `false`, read the same way as
  `create_cogs`: `self._payload.get("<flag>", False)` (`task.py`,
  `process()`). Keep it next to `create_cogs`. If task config later moves
  to `process.tasks.<task-name>`, move both flags together.
- **Hook point**: the one return in `process()` that emits items:
  ```python
  out = self.add_software_version_to_item(item.to_dict())
  return [downgrade_item(out) if <flag> else out]
  ```
  Call it after `add_software_version_to_item`, which the converter leaves
  alone. The early `return []` needs nothing.
- **Why here**:
  - The task never writes the item JSON itself (Cirrus publishes the
    return value), so this is the only point where output leaves.
  - The conversion works on the final output only, so it composes with
    every build mode (full rebuild, reuse of the existing doc, COGs on or
    off) without changing any of them.
- **Rejected alternatives**:
  - *Inline in the build steps*: pystac 2.0 always serializes as 1.1, the
    logic would spread across `stac.py` / `task.py` / `cogify.py`, and
    `cogify.py` reads band fields in the 1.1 shape mid-pipeline.
  - *Separate Cirrus task*: an extra Lambda and payload round trip for a
    pure dict transform. Only worth it if the conversion needs its own
    deploy cycle or has to apply to other tasks' items.
- **Freshness check**: `is_newer_than_existing` stays pointed at the v2 API
  in both modes (the single source of truth, per the project design).
  Revisit only if v1 output gets published to a separate v1 catalog.
- **Item input**: if a pystac `Item` is ever needed as input,
  `downgrade_item(item.to_dict())` is enough. Keep the result as a dict,
  because serializing through pystac again stamps `1.1.0` back on.

## Checklist for the real change

- [ ] Move the module to `src/sentinel_2_l2a_to_stac/downgrade.py` and drop
      `__main__` (it already passes `mypy --strict` and ruff).
- [ ] Add the flag and the hook in `process()`.
- [ ] Add a new success fixture directory with the flag set and a v1-shaped
      `out.json`. The existing fixtures stay unchanged.
- [ ] Add a README "Configuration Parameters" row (type, optional, default
      `false`).
- [ ] Add a CHANGELOG entry under `[Unreleased]`.
- [ ] Decide raise vs. warn-and-drop for unknown band fields and for
      storage schemes that disagree. The POC raises.
