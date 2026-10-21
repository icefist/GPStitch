"""Choose a map's zoom from how fast you are going, and lay out what it shows.

Slow is a village street and wants to be close; fast is a main road and wants
the next few kilometres. This module holds the arithmetic for that - the speed,
the zoom it implies, where the view sits on the map's tile grid, and which bit
of the route falls inside it - with no drawing and no tiles, so all of it can be
tested without a network.
"""

import bisect
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise

_EARTH_RADIUS_M = 6_371_000.0

# Pixels per tile edge on the Web Mercator grid every tile server uses.
_TILE_PX = 256

# A fractional zoom draws the next whole level up and shrinks it. Without the
# slack, 15.0000000001 - an exact 15 after floating-point arithmetic - would
# fetch level 16 and halve it.
_ZOOM_EPSILON = 1e-6


def zoom_for_speed(kph: float, *, zoom_slow: float, zoom_fast: float, slow_kph: float, fast_kph: float) -> float:
    """The zoom for `kph`: `zoom_slow` at or below `slow_kph`, `zoom_fast` at or above `fast_kph`.

    In between, each doubling of speed moves the zoom by the same amount. Zoom
    levels are logarithmic - one level halves the distance shown - so a speed
    scale that was linear would do nearly all its zooming in the first few km/h.
    """
    if kph <= slow_kph:
        return zoom_slow
    if kph >= fast_kph:
        return zoom_fast
    share = math.log(kph / slow_kph) / math.log(fast_kph / slow_kph)
    return zoom_slow + (zoom_fast - zoom_slow) * share


def _metres_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


class SpeedTrack:
    """Speed at any moment of a ride, smoothed over a window of `window_s` seconds.

    The speed is how far you got across the window divided by its length - the
    speed of your averaged position, not the average of your speeds. The two
    differ exactly where it matters: a single fix thrown fifty metres sideways
    adds a hundred metres of travel out and back, which averaging speeds would
    report as a burst of pace, while where you got to barely moves.

    Readings come from positions interpolated between fixes, so they change
    continuously with time - one GPS fix a second still gives a different, and
    smoothly different, reading on every frame. With `window_s` of 0 it falls
    back to the speed between the two fixes either side.
    """

    def __init__(self, samples: list[tuple[datetime, float, float]], window_s: float):
        self.window_s = window_s
        self._seconds: list[float] = []
        self._lats: list[float] = []
        self._lons: list[float] = []
        self._start: datetime | None = samples[0][0] if samples else None

        for when, lat, lon in samples:
            t = (when - self._start).total_seconds()
            # A repeated timestamp would put two places at one moment; keep the first.
            if self._seconds and t <= self._seconds[-1]:
                continue
            self._seconds.append(t)
            self._lats.append(lat)
            self._lons.append(lon)

    def kph_at(self, when: datetime) -> float | None:
        """The speed at `when`, or None if the track is too short to have one."""
        if len(self._seconds) < 2:
            return None

        first, last = self._seconds[0], self._seconds[-1]
        t = min(max((when - self._start).total_seconds(), first), last)

        half = self.window_s / 2
        if half <= 0:
            i = min(max(bisect.bisect_right(self._seconds, t) - 1, 0), len(self._seconds) - 2)
            a, b = self._seconds[i], self._seconds[i + 1]
        else:
            # Near the ends the window shrinks rather than reaching past the track.
            a, b = max(first, t - half), min(last, t + half)
            if b - a <= 0:
                return None

        lat_a, lon_a = self._position_at(a)
        lat_b, lon_b = self._position_at(b)
        return _metres_between(lat_a, lon_a, lat_b, lon_b) / (b - a) * 3.6

    def _position_at(self, t: float) -> tuple[float, float]:
        i = bisect.bisect_right(self._seconds, t) - 1
        if i >= len(self._seconds) - 1:
            return self._lats[-1], self._lons[-1]
        i = max(i, 0)
        t0, t1 = self._seconds[i], self._seconds[i + 1]
        share = (t - t0) / (t1 - t0)
        return (
            self._lats[i] + (self._lats[i + 1] - self._lats[i]) * share,
            self._lons[i] + (self._lons[i + 1] - self._lons[i]) * share,
        )


def world_px(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    """Where a position falls on the whole-world Web Mercator image at `zoom`."""
    size = _TILE_PX * 2**zoom
    s = math.sin(math.radians(lat))
    return (
        (lon + 180.0) / 360.0 * size,
        (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * size,
    )


def world_lonlat(x: float, y: float, zoom: int) -> tuple[float, float]:
    """The position at a pixel of the whole-world image at `zoom`."""
    size = _TILE_PX * 2**zoom
    return (
        x / size * 360.0 - 180.0,
        math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / size)))),
    )


@dataclass(frozen=True)
class View:
    """A square of the map centred on a position, at a possibly fractional zoom.

    Tiles exist only at whole zoom levels, so the view is drawn from the next
    level up (`tile_zoom`) and shrunk by `factor`: `extent` pixels of that level
    become the widget's `size`. Shrinking sharper tiles keeps the map crisp;
    enlarging coarser ones would blur it.
    """

    zoom: float
    tile_zoom: int
    factor: float
    origin_x: float
    origin_y: float
    extent: float

    def to_output(self, x: float, y: float) -> tuple[float, float]:
        """A pixel of the `tile_zoom` world image, in the widget's own pixels."""
        return (x - self.origin_x) / self.factor, (y - self.origin_y) / self.factor


def view_for(lon: float, lat: float, zoom: float, *, size: int) -> View:
    """The view of `size` pixels centred on (lon, lat) at `zoom`."""
    tile_zoom = math.ceil(zoom - _ZOOM_EPSILON)
    factor = 2 ** (tile_zoom - zoom)
    extent = size * factor
    x, y = world_px(lon, lat, tile_zoom)
    return View(
        zoom=zoom,
        tile_zoom=tile_zoom,
        factor=factor,
        origin_x=x - extent / 2,
        origin_y=y - extent / 2,
        extent=extent,
    )


def chunks_covering(origin_x: float, origin_y: float, extent: float, *, chunk: int) -> list[tuple[int, int]]:
    """The (column, row) of every `chunk`-pixel square a view overlaps."""
    # A view ending exactly on a chunk edge does not reach into the next one.
    end_x, end_y = origin_x + extent - 1e-9, origin_y + extent - 1e-9
    return [
        (cx, cy)
        for cy in range(math.floor(origin_y / chunk), math.floor(end_y / chunk) + 1)
        for cx in range(math.floor(origin_x / chunk), math.floor(end_x / chunk) + 1)
    ]


class RouteIndex:
    """A route in world pixels, indexed so a view can fetch just the part it shows.

    A three-hour ride at ten points a second is tens of thousands of points, and
    a view at street level shows a few hundred metres of it. Drawing all of them
    every frame is where the time would go.
    """

    def __init__(self, points: list[tuple[float, float]], *, cell: int, min_step: float = 1.0):
        self._cell = cell
        self._points = _thinned(points, min_step)
        self._segments_by_cell: dict[tuple[int, int], list[int]] = defaultdict(list)

        for i, ((x1, y1), (x2, y2)) in enumerate(pairwise(self._points)):
            for cy in range(math.floor(min(y1, y2) / cell), math.floor(max(y1, y2) / cell) + 1):
                for cx in range(math.floor(min(x1, x2) / cell), math.floor(max(x1, x2) / cell) + 1):
                    self._segments_by_cell[(cx, cy)].append(i)

    @property
    def point_count(self) -> int:
        return len(self._points)

    def runs_in(self, x0: float, y0: float, x1: float, y1: float) -> list[list[tuple[float, float]]]:
        """The stretches of route touching the rectangle, each as an unbroken polyline."""
        segments: set[int] = set()
        for cy in range(math.floor(y0 / self._cell), math.floor(y1 / self._cell) + 1):
            for cx in range(math.floor(x0 / self._cell), math.floor(x1 / self._cell) + 1):
                segments.update(self._segments_by_cell.get((cx, cy), ()))

        runs: list[list[tuple[float, float]]] = []
        start = previous = None
        for i in sorted(segments):
            if previous is not None and i == previous + 1:
                previous = i
                continue
            if start is not None:
                runs.append(self._points[start : previous + 2])
            start = previous = i
        if start is not None:
            runs.append(self._points[start : previous + 2])
        return runs


def _thinned(points: list[tuple[float, float]], min_step: float) -> list[tuple[float, float]]:
    """Drop points closer than `min_step` pixels to the last kept one, keeping the ends."""
    if len(points) < 3:
        return list(points)
    kept = [points[0]]
    for x, y in points[1:-1]:
        kx, ky = kept[-1]
        if (x - kx) ** 2 + (y - ky) ** 2 >= min_step**2:
            kept.append((x, y))
    kept.append(points[-1])
    return kept
