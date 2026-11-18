"""Band metadata accessors: the only module that knows how STAC 1.1 `bands`
are stored on a pystac object.

Callers exchange plain JSON-shaped dicts (keys as they appear in the STAC 1.1
`bands` array, e.g. ``name``, ``eo:common_name``, ``raster:scale``) so that
the storage mechanism behind these functions can change without touching
call sites.

SHIM(pystac-bands): pystac 1.16 has no native ``Asset.bands``, so bands are
stored as raw dicts in ``asset.extra_fields["bands"]``, and the eo/raster
extension URIs are bumped to v2.0.0 by hand. Keep every read/write of an
asset's bands and of those URIs in this module, so adopting pystac's native
bands API is a change to this file only. See DEVELOPMENT.md, "Removing the
bands shim".
"""

from typing import Any, Sequence

import pystac

from sentinel_2_l2a_to_stac.constants import EO_EXT_V2, RASTER_EXT_V2

BANDS_KEY = "bands"
_EO_EXT_PREFIX = EO_EXT_V2.rsplit("/v", 1)[0] + "/"
_RASTER_EXT_PREFIX = RASTER_EXT_V2.rsplit("/v", 1)[0] + "/"


def get_asset_bands(asset: pystac.Asset) -> list[dict[str, Any]] | None:
    """Return the asset's own band dicts, or None if it has none.

    Only the asset's own bands are returned; nothing is inherited from the
    owning Item. The list is a new list, but the dicts are the stored ones:
    treat them as read-only and write changes back with `set_asset_bands`.
    """
    bands = asset.extra_fields.get(BANDS_KEY)
    if not bands:
        return None
    return list(bands)


def set_asset_bands(
    asset: pystac.Asset, bands: list[dict[str, Any]] | None
) -> None:
    """Replace the asset's bands with the given dicts (None removes them).

    The asset takes ownership of the dicts; don't mutate them afterwards.
    """
    if bands is None:
        asset.extra_fields.pop(BANDS_KEY, None)
    else:
        asset.extra_fields[BANDS_KEY] = list(bands)


def apply_asset_bands(asset: pystac.Asset, bands: list[dict[str, Any]]) -> None:
    """Write band metadata to an asset in STAC 1.1 canonical form.

    Single-band assets get the band's fields merged directly onto the asset.
    Multi-band assets have fields that are identical across every band moved
    up to the asset; only band-specific fields remain in the asset's `bands`.
    """
    if len(bands) == 1:
        asset.extra_fields.update(bands[0])
        return

    first = bands[0]
    common = {
        field: value
        for field, value in first.items()
        if all(other.get(field) == value for other in bands[1:])
    }
    if common:
        asset.extra_fields.update(common)
        bands = [
            {field: value for field, value in band.items() if field not in common}
            for band in bands
        ]
    set_asset_bands(asset, bands)


def resolve_asset_band_fields(
    asset: pystac.Asset, keys: Sequence[str]
) -> list[dict[str, Any]] | None:
    """Return each band's effective value for `keys`, undoing `apply_asset_bands`.

    Multi-band: one dict per band; a key the band lacks falls back to the
    asset-level value. Single-band (no `bands` array): one dict read from the
    asset's own fields, or None if the asset carries none of `keys`. Missing
    values are None.
    """
    fields = asset.extra_fields
    bands = get_asset_bands(asset)
    if bands:
        return [
            {key: band[key] if key in band else fields.get(key) for key in keys}
            for band in bands
        ]
    if not any(key in fields for key in keys):
        return None
    return [{key: fields.get(key) for key in keys}]


def ensure_band_extension_versions(item: pystac.Item) -> None:
    """Replace eo/raster extension URIs with the v2.0.0 schemas that define the
    STAC 1.1.0 `bands` field shape."""
    item.stac_extensions = [
        EO_EXT_V2
        if ext.startswith(_EO_EXT_PREFIX)
        else RASTER_EXT_V2
        if ext.startswith(_RASTER_EXT_PREFIX)
        else ext
        for ext in (item.stac_extensions or [])
    ]
