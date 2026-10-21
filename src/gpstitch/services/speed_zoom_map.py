"""A journey map that follows you: close when slow, wide when fast.

Through a village you want to see which street you are on; on a main road you
want the next few kilometres. The map stays centred on your position and its
zoom follows your speed, read from the GPS track itself (see speed_zoom).

The obvious way to draw a moving map is to ask the tile renderer for a fresh
one every frame, which is what gopro-overlay's own moving_map does. A render
costs ~800ms even from the tile cache, so over a three-hour ride that is days.
Here the map is rendered in tile-aligned chunks (~115ms each, once) and each
frame is cut from the chunks around you and scaled to the exact fractional zoom
- about 5ms. Only the route line and the marker are drawn fresh, and only the
stretch of route the view can see.
"""

import logging
from collections import OrderedDict

import geotiler
from gopro_overlay.gpmf import GPS_FIXED_VALUES
from gopro_overlay.journey import Journey
from gopro_overlay.widgets.map import draw_marker
from gopro_overlay.widgets.widgets import Widget
from PIL import Image, ImageDraw

from gpstitch.services.speed_zoom import (
    RouteIndex,
    SpeedTrack,
    chunks_covering,
    view_for,
    world_lonlat,
    world_px,
    zoom_for_speed,
)

logger = logging.getLogger(__name__)

# 2x2 tiles. Aligned to the tile grid a chunk costs ~115ms to render; the same
# area unaligned costs ~800ms, because the renderer then fetches the tiles all
# around it as well.
_CHUNK_PX = 512

# 64 chunks of 512px RGBA is 64MB. A ride moves steadily along, so the chunks
# behind it are the ones that can go.
_MAX_CHUNKS = 64

# What a chunk looks like when the tile server cannot be reached: plain, and
# obviously not a map, rather than a failed render.
_UNREACHABLE = (200, 200, 200, 255)

_ROUTE = (255, 0, 0)
_ROUTE_WIDTH = 4
_MARKER_RADIUS = 6


class SpeedZoomJourneyMap(Widget):
    """A journey map centred on you, its zoom following your speed."""

    def __init__(
        self,
        *,
        framemeta,
        entry,
        renderer,
        privacy,
        size: int,
        border,
        zoom_slow: float,
        zoom_fast: float,
        slow_kph: float,
        fast_kph: float,
        smoothing_s: float,
        chunk: int = _CHUNK_PX,
        max_chunks: int = _MAX_CHUNKS,
    ):
        self.framemeta = framemeta
        self.entry = entry
        self.renderer = renderer
        self.privacy = privacy
        self.size = size
        self.border = border
        self.zoom_slow = zoom_slow
        self.zoom_fast = zoom_fast
        self.slow_kph = slow_kph
        self.fast_kph = fast_kph
        self.smoothing_s = smoothing_s
        self.chunk = chunk
        self.max_chunks = max_chunks

        self.last_zoom: float | None = None
        self._track: SpeedTrack | None = None
        self._routes: dict[int, RouteIndex] = {}
        self._chunks: OrderedDict[tuple[int, int, int], Image.Image] = OrderedDict()
        self._canvas: Image.Image | None = None
        self._canvas_key: tuple | None = None
        self._last: tuple[float, float, float] | None = None

    @property
    def cached_chunks(self) -> int:
        return len(self._chunks)

    def draw(self, image, draw):
        current = self.entry()
        point = getattr(current, "point", None)

        if point is not None and point.lat is not None and point.lon is not None:
            zoom = self._zoom_at(getattr(current, "dt", None))
            self._last = (point.lon, point.lat, zoom)
        elif self._last is None:
            # Nothing to centre on yet. An empty corner beats a failed render.
            return
        # Otherwise a dropped fix: keep showing where we last were rather than
        # blanking the map, which would flicker with every gap in the GPS.

        lon, lat, zoom = self._last
        self.last_zoom = zoom
        image.alpha_composite(self._frame(lon, lat, zoom), self.at.tuple())

    # --- zoom -------------------------------------------------------------

    def _zoom_at(self, when) -> float:
        if self._track is None:
            self._track = SpeedTrack(
                [(e.dt, e.point.lat, e.point.lon) for e in self._fixes()],
                window_s=self.smoothing_s,
            )
        kph = self._track.kph_at(when) if when is not None else None
        if kph is None:
            # No movement to measure - a lone fix, or a clip too short to have
            # one. Close in is the useful view of standing still.
            return self.zoom_slow
        return zoom_for_speed(
            kph,
            zoom_slow=self.zoom_slow,
            zoom_fast=self.zoom_fast,
            slow_kph=self.slow_kph,
            fast_kph=self.fast_kph,
        )

    def _fixes(self) -> list:
        """Entries with a usable position, in time order - unlocked fixes are noise."""
        frames = self.framemeta.frames
        fixes = []
        for key in sorted(frames):
            entry = frames[key]
            point = getattr(entry, "point", None)
            if point is None or point.lat is None or point.lon is None:
                continue
            if getattr(entry, "gpsfix", None) not in GPS_FIXED_VALUES:
                continue
            fixes.append(entry)
        return fixes

    # --- drawing ----------------------------------------------------------

    def _frame(self, lon: float, lat: float, zoom: float) -> Image.Image:
        view = view_for(lon, lat, zoom, size=self.size)
        chunk = self.chunk
        keys = chunks_covering(view.origin_x, view.origin_y, view.extent, chunk=chunk)
        left = min(cx for cx, _ in keys)
        top = min(cy for _, cy in keys)

        # Riding along moves the view a pixel or two a frame, so it usually still
        # sits on the same chunks - reuse the canvas rather than re-pasting them.
        canvas_key = (view.tile_zoom, *sorted(keys))
        if canvas_key != self._canvas_key:
            columns = max(cx for cx, _ in keys) - left + 1
            rows = max(cy for _, cy in keys) - top + 1
            self._canvas = Image.new("RGBA", (columns * chunk, rows * chunk))
            for cx, cy in keys:
                self._canvas.paste(self._chunk(view.tile_zoom, cx, cy), ((cx - left) * chunk, (cy - top) * chunk))
            self._canvas_key = canvas_key

        # The box is fractional, so panning and zooming move smoothly rather
        # than in whole pixels of the larger tile level.
        box_x, box_y = view.origin_x - left * chunk, view.origin_y - top * chunk
        frame = self._canvas.resize(
            (self.size, self.size),
            Image.Resampling.BILINEAR,
            box=(box_x, box_y, box_x + view.extent, box_y + view.extent),
        )

        pen = ImageDraw.Draw(frame)
        route = self._route(view.tile_zoom)
        for run in route.runs_in(
            view.origin_x, view.origin_y, view.origin_x + view.extent, view.origin_y + view.extent
        ):
            if len(run) > 1:
                pen.line([view.to_output(x, y) for x, y in run], fill=_ROUTE, width=_ROUTE_WIDTH, joint="curve")
        draw_marker(pen, (self.size / 2, self.size / 2), _MARKER_RADIUS)

        return self.border.rounded(frame)

    def _route(self, tile_zoom: int) -> RouteIndex:
        if tile_zoom not in self._routes:
            journey = Journey()
            self.framemeta.process(journey.accept)
            points = [world_px(p.lon, p.lat, tile_zoom) for p in journey.locations if not self.privacy.encloses(p)]
            self._routes[tile_zoom] = RouteIndex(points, cell=self.chunk)
        return self._routes[tile_zoom]

    def _chunk(self, tile_zoom: int, cx: int, cy: int) -> Image.Image:
        key = (tile_zoom, cx, cy)
        if key in self._chunks:
            self._chunks.move_to_end(key)
            return self._chunks[key]

        centre = world_lonlat((cx + 0.5) * self.chunk, (cy + 0.5) * self.chunk, tile_zoom)
        try:
            image = self.renderer(geotiler.Map(center=centre, zoom=tile_zoom, size=(self.chunk, self.chunk)))
            image = image.convert("RGBA")
        except Exception as e:  # noqa: BLE001 - a missing map must not fail the whole render
            # Kept, so the next frame does not wait out the same timeout again.
            logger.warning("Could not render map at zoom %s (%s, %s): %s", tile_zoom, cx, cy, e)
            image = Image.new("RGBA", (self.chunk, self.chunk), _UNREACHABLE)

        self._chunks[key] = image
        while len(self._chunks) > self.max_chunks:
            self._chunks.popitem(last=False)
        return image
