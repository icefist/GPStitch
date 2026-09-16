"""Resolve a whole trip's place names before the frame loop runs.

Place names deliberately do NOT live on Entry objects: Entry.interpolate
subtracts every field and catches only KeyError, so a string field raises
TypeError - and FrameMeta.get interpolates on nearly every frame. A separate
time-indexed track sidesteps that entirely.

Sampling refines progressively: eleven samples spread across the whole trip
first, then bisection of only those intervals whose endpoints resolved to
different names. Every level is complete in itself, so abandoning the process -
budget exhausted, API down - leaves a usable coarser track rather than a
half-resolved one.
"""

import bisect
import logging
from dataclasses import dataclass
from datetime import datetime

from gpstitch.services.place_resolver import Backend, PlaceName

# The names that mean somewhere built up. The rest of the hierarchy -
# municipality, county, state, country - resolves on open road too.
_SETTLEMENT_FIELDS = ("village", "town", "city")

logger = logging.getLogger(__name__)

# Nominal speed used to convert the metre target into a time threshold for
# tracks that carry no distance data. ~50 km/h sits between hiking and driving;
# the feature targets coarse accuracy, so a rough equivalence is fine.
_NOMINAL_SPEED_MS = 13.9


@dataclass(frozen=True)
class PlaceSample:
    """One resolved point.

    `backend` is retained because refinement must never compare names that came
    from different sources: they word things differently, which would look like
    a boundary that is not there.
    """

    dt: datetime
    place: PlaceName | None
    backend: Backend


class PlaceTrack:
    """Time-indexed place names with last-known-value lookup."""

    def __init__(self, samples: list[PlaceSample]):
        self.samples = sorted(samples, key=lambda s: s.dt)
        self._dts = [s.dt for s in self.samples]

    def at(self, dt: datetime | None) -> str:
        """Name in effect at `dt`. Always a str, never None."""
        place = self.place_at(dt)
        return place.display_name() if place is not None else ""

    def place_at(self, dt: datetime | None) -> PlaceName | None:
        """The resolved hierarchy in effect at `dt`, unflattened.

        `at()` loses the distinction between a city and the province sharing its
        name, which callers that care about *what kind* of place this is - the
        map, deciding whether to zoom - need to keep.
        """
        if not self.samples or dt is None:
            return None
        idx = bisect.bisect_right(self._dts, dt) - 1
        if idx < 0:
            idx = 0  # before the first sample: hold the first known name
        return self.samples[idx].place

    def in_settlement(self, dt: datetime | None) -> bool:
        """Whether `dt` falls inside a village, town or city.

        The wider names - county, state, country - resolve everywhere, including
        open road, so they say nothing about being somewhere built up.
        """
        place = self.place_at(dt)
        if place is None:
            return False
        return any(getattr(place, field, None) for field in _SETTLEMENT_FIELDS)


def _ordered_entries(framemeta) -> list:
    """Entries in time order, skipping any without a usable GPS point."""
    entries = [framemeta.frames[k] for k in sorted(framemeta.frames)]
    return [e for e in entries if getattr(e, "point", None) is not None]


def _progress_axis(entries: list, target_metres: float) -> tuple[list[float], float]:
    """Return (axis values, stop threshold expressed in the axis's own units).

    Prefers cumulative distance in metres. Falls back to elapsed seconds when
    codo is missing - and converts the target too, so the stop condition never
    compares seconds against metres.
    """
    codos = []
    for e in entries:
        codo = getattr(e, "codo", None)
        if codo is None:
            codos = []
            break
        codos.append(float(getattr(codo, "magnitude", codo)))
    if codos and codos[-1] > 0:
        return codos, float(target_metres)
    t0 = entries[0].dt
    return [(e.dt - t0).total_seconds() for e in entries], target_metres / _NOMINAL_SPEED_MS


def build_place_track(
    framemeta,
    resolver,
    lang: str,
    target_metres: int,
    initial_samples: int,
    max_lookups: int,
) -> PlaceTrack:
    """Resolve a trip's place names, coarse to fine.

    `resolver.resolve(lat, lon, lang)` must return `(PlaceName | None, Backend)`.
    """
    entries = _ordered_entries(framemeta)
    if not entries:
        return PlaceTrack([])

    axis, stop_threshold = _progress_axis(entries, target_metres)
    budget = [max_lookups]
    resolved: dict[int, PlaceSample] = {}

    def sample(idx: int) -> PlaceSample | None:
        """Resolve entry `idx` once, honouring the budget."""
        if idx in resolved:
            return resolved[idx]
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        entry = entries[idx]
        try:
            place, backend = resolver.resolve(entry.point.lat, entry.point.lon, lang)
        except Exception as e:  # noqa: BLE001 - a failed lookup never fails a render
            logger.info("Place lookup failed at index %d: %s", idx, e)
            return None
        s = PlaceSample(entry.dt, place, backend)
        resolved[idx] = s
        return s

    # --- Level 0: spread `initial_samples` evenly, covering the whole trip.
    n = len(entries)
    count = max(1, min(initial_samples, n))
    step = (n - 1) / (count - 1) if count > 1 else 0.0
    level0 = sorted({round(i * step) for i in range(count)})
    for idx in level0:
        sample(idx)

    # --- Refinement: bisect only intervals whose endpoints disagree.
    pending = [(level0[i], level0[i + 1]) for i in range(len(level0) - 1)]
    while pending and budget[0] > 0:
        lo, hi = pending.pop(0)
        s_lo, s_hi = resolved.get(lo), resolved.get(hi)
        if s_lo is None or s_hi is None:
            continue
        # Never compare names that came from different sources.
        if s_lo.backend is not s_hi.backend:
            continue
        if s_lo.place == s_hi.place:
            continue
        interval = axis[hi] - axis[lo]
        if interval <= stop_threshold or hi - lo <= 1:
            continue
        mid = (lo + hi) // 2
        if sample(mid) is None:
            continue
        pending.append((lo, mid))
        pending.append((mid, hi))

    return PlaceTrack(list(resolved.values()))
