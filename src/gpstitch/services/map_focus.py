"""Decide what a single map should be framed on, moment to moment.

A journey map drawn over a whole ride makes a town an unreadable smudge - at
100km across, a position within a village is a few pixels. Framing the map on
the part of the route that runs through the current settlement makes it legible,
and going back to the whole route between places keeps the overview.

Only the choice and the framing live here. The drawing is the widget's job, and
keeping the two apart means this can be tested without fetching a single tile.
"""

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def route_bounds(points) -> tuple[float, float, float, float] | None:
    """(min_lat, min_lon, max_lat, max_lon) spanning `points`, or None if empty."""
    lats = [p.lat for p in points]
    lons = [p.lon for p in points]
    if not lats:
        return None
    return (min(lats), min(lons), max(lats), max(lons))


class MapFocus:
    """Which settlement, if any, the map should be framed on right now.

    Leaving a settlement is smoothed by a dwell: a road clipping the edge of a
    village would otherwise zoom in and straight back out within a kilometre.
    Arriving somewhere new is not smoothed - that is information, not noise.
    """

    def __init__(self, track, dwell_s: float):
        self.track = track
        self.dwell_s = dwell_s
        self._held: str | None = None
        self._last_inside: datetime | None = None
        self._bounds_cache: dict[str, tuple[float, float, float, float] | None] = {}

    def settlement_at(self, dt: datetime | None) -> str | None:
        """The settlement to frame on, or None for the whole route."""
        if dt is None:
            return None

        place = self.track.place_at(dt) if self.track.in_settlement(dt) else None
        current = place.display_name() if place is not None else None

        if current:
            self._held = current
            self._last_inside = dt
            return current

        # Outside a settlement: hold the last one briefly before zooming out.
        # Measured from the last moment actually inside rather than from when
        # this first noticed, so the dwell does not depend on being asked every
        # frame.
        if self._held is None or self._last_inside is None:
            return None
        if (dt - self._last_inside) < timedelta(seconds=self.dwell_s):
            return self._held

        self._held = None
        self._last_inside = None
        return None

    def bounds_for(self, entries, settlement: str) -> tuple[float, float, float, float] | None:
        """Bounds of the route where it runs through `settlement`.

        Computed once per settlement: scanning the whole track on every frame
        would cost more than the drawing does.
        """
        if settlement in self._bounds_cache:
            return self._bounds_cache[settlement]

        inside = []
        for entry in entries:
            place = self.track.place_at(getattr(entry, "dt", None))
            if place is not None and place.display_name() == settlement:
                point = getattr(entry, "point", None)
                if point is not None:
                    inside.append(point)

        bounds = route_bounds(inside)
        if bounds is None:
            logger.debug("No route points resolved inside %s", settlement)
        self._bounds_cache[settlement] = bounds
        return bounds
