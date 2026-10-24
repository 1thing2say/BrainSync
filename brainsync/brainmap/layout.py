"""Where the Cerelog X8 electrodes sit on a flat drawing of the head, and what lies under them.

Positions are the 10-20 sites in an azimuthal-equidistant projection centred on Cz: the
distance from the centre is the angle from Cz, scaled so the head circle (the 90-degree
level through the ears, inion and nasion) has radius 1.0; x points to the right ear and y
to the nose. The outer 10-20 ring (Fp, T/P7-P8, O) lies at 72 degrees from Cz, 10% of the
nasion-inion arc above that level, so at radius 0.8; C3/C4 lie halfway between Cz and that
ring, at radius 0.4. This is the layout EEG topographic maps conventionally use.

The regions only name the cortex closest to each electrode, for labels. Eight scalp sites
cannot locate a source inside the brain, so nothing here should be read as one.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

OUTER_RING_DEG = 72.0   # Fp, P7/P8, O: 72 degrees from Cz
INNER_RING_DEG = 36.0   # C3/C4
HEAD_DEG = 90.0         # the head circle drawn at radius 1.0


def _site(from_cz_deg: float, azimuth_deg: float) -> tuple[float, float]:
    """(x, y) for a site ``from_cz_deg`` from Cz, ``azimuth_deg`` clockwise from the nose."""
    r = from_cz_deg / HEAD_DEG
    a = math.radians(azimuth_deg)
    return round(r * math.sin(a), 3) + 0.0, round(r * math.cos(a), 3) + 0.0


SITES: dict[str, tuple[float, float]] = {
    "Fp1": _site(OUTER_RING_DEG, -18.0),
    "Fp2": _site(OUTER_RING_DEG, 18.0),
    "C3": _site(INNER_RING_DEG, -90.0),
    "C4": _site(INNER_RING_DEG, 90.0),
    "P7": _site(OUTER_RING_DEG, -126.0),
    "P8": _site(OUTER_RING_DEG, 126.0),
    "O1": _site(OUTER_RING_DEG, -162.0),
    "O2": _site(OUTER_RING_DEG, 162.0),
}

REGIONS: dict[str, str] = {
    "Fp1": "prefrontal", "Fp2": "prefrontal",
    "C3": "sensorimotor", "C4": "sensorimotor",
    "P7": "temporal-parietal", "P8": "temporal-parietal",
    "O1": "visual", "O2": "visual",
}

_CANONICAL = {name.lower(): name for name in SITES}


def site(name: str) -> str:
    """The SITES key for ``name`` (case-insensitive); ValueError for a site not mapped."""
    found = _CANONICAL.get(str(name).strip().lower())
    if found is None:
        raise ValueError(f"no head position for electrode {name!r}; known sites: {list(SITES)}")
    return found


def positions(ch_names: Sequence[str]) -> dict[str, list[float]]:
    """{name: [x, y]} in head units for each channel, in the given order."""
    return {str(n): list(SITES[site(n)]) for n in ch_names}


def regions(ch_names: Sequence[str]) -> dict[str, str]:
    """{name: region under the electrode} for each channel, in the given order."""
    return {str(n): REGIONS[site(n)] for n in ch_names}
