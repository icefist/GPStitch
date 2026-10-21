"""The journey map that follows you, closer when slow and wider when fast.

These draw for real, but against a stand-in tile renderer: what matters here is
where things land and how often the expensive work is done, not what the tiles
look like.
"""

from datetime import UTC, datetime, timedelta

from gopro_overlay.point import Coordinate
from gopro_overlay.widgets.map import MaybeRoundedBorder
from PIL import Image

from gpstitch.services.speed_zoom_map import SpeedZoomJourneyMap

T0 = datetime(2026, 9, 15, 17, 0, 0, tzinfo=UTC)
LAT, LON = 53.2194, 6.5665
M_PER_DEG_LAT = 111_195.0
SIZE = 200
AT = (40, 30)
LOCKED = 3


class FakePoint:
    def __init__(self, lat, lon):
        self.lat, self.lon = lat, lon


class FakeEntry:
    def __init__(self, dt, lat, lon, gpsfix=LOCKED):
        self.dt = dt
        self.point = FakePoint(lat, lon) if lat is not None else None
        self.gpsfix = gpsfix


class FakeFrameMeta:
    def __init__(self, entries):
        self.frames = dict(enumerate(entries))

    def process(self, fn):
        for key in sorted(self.frames):
            fn(self.frames[key])


class NoPrivacy:
    def encloses(self, point):
        return False


class FakeRenderer:
    """A plain white map, counting how often it is asked for one."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, map_):
        self.calls.append((map_.zoom, map_.center))
        if self.fail:
            raise OSError("tile server unreachable")
        return Image.new("RGBA", map_.size, (255, 255, 255, 255))


def ride(kph, seconds=60, rate_hz=10):
    """Northward at a steady speed, sampled like the overlay samples it."""
    step = (kph / 3.6) / M_PER_DEG_LAT / rate_hz
    return [FakeEntry(T0 + timedelta(seconds=i / rate_hz), LAT + i * step, LON) for i in range(seconds * rate_hz + 1)]


class Harness:
    def __init__(self, entries, renderer=None, **overrides):
        self.entries = entries
        self.current = entries[0]
        self.renderer = renderer or FakeRenderer()
        options = {
            "zoom_slow": 16.0,
            "zoom_fast": 12.0,
            "slow_kph": 20.0,
            "fast_kph": 110.0,
            "smoothing_s": 4.0,
            **overrides,
        }
        self.widget = SpeedZoomJourneyMap(
            framemeta=FakeFrameMeta(entries),
            entry=lambda: self.current,
            renderer=self.renderer,
            privacy=NoPrivacy(),
            size=SIZE,
            border=MaybeRoundedBorder(size=SIZE, corner_radius=0, opacity=1.0),
            **options,
        )
        self.widget.at = Coordinate(*AT)

    def draw_at(self, index):
        self.current = self.entries[index]
        canvas = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
        self.widget.draw(canvas, None)
        return canvas


def centre(canvas, dx=0, dy=0):
    return canvas.getpixel((AT[0] + SIZE // 2 + dx, AT[1] + SIZE // 2 + dy))


class TestWhatItDraws:
    def test_the_map_lands_at_the_widget_position_and_nowhere_else(self):
        canvas = Harness(ride(50)).draw_at(300)

        assert canvas.getpixel((AT[0] + 5, AT[1] + 5))[3] == 255
        assert canvas.getpixel((AT[0] - 5, AT[1] - 5))[3] == 0
        assert canvas.getpixel((AT[0] + SIZE + 5, AT[1] + SIZE + 5))[3] == 0

    def test_you_are_the_marker_in_the_middle(self):
        r, g, b, _ = centre(Harness(ride(50)).draw_at(300))

        assert (r, g, b) == (0, 0, 255)

    def test_the_route_runs_through_you(self):
        """Heading north: the route leaves the marker straight up and down."""
        canvas = Harness(ride(50)).draw_at(300)

        r, g, b, _ = centre(canvas, dy=-30)
        assert (r, g, b) == (255, 0, 0)
        r, g, b, _ = centre(canvas, dx=-60)
        assert (r, g, b) == (255, 255, 255)


class TestZoom:
    def test_crawling_is_fully_zoomed_in(self):
        harness = Harness(ride(8))
        harness.draw_at(300)

        assert harness.widget.last_zoom == 16.0

    def test_flat_out_is_fully_zoomed_out(self):
        harness = Harness(ride(150))
        harness.draw_at(300)

        assert harness.widget.last_zoom == 12.0

    def test_in_between_it_is_in_between(self):
        harness = Harness(ride(50))
        harness.draw_at(300)

        assert 12.0 < harness.widget.last_zoom < 16.0

    def test_with_no_movement_to_measure_it_stays_zoomed_in(self):
        harness = Harness([FakeEntry(T0, LAT, LON)])
        harness.draw_at(0)

        assert harness.widget.last_zoom == 16.0


class TestCost:
    def test_map_tiles_are_rendered_once_not_once_per_frame(self):
        """A rendered chunk costs ~115ms; a three-hour ride is 100,000 frames."""
        harness = Harness(ride(30))

        for index in range(200, 300):
            harness.draw_at(index)

        chunks = set(harness.renderer.calls)
        assert len(harness.renderer.calls) == len(chunks)
        assert len(chunks) <= 9

    def test_memory_for_chunks_is_bounded(self):
        # Small chunks, so a ten-minute ride crosses many more than the cache holds.
        harness = Harness(ride(150, seconds=600), max_chunks=4, chunk=64)

        for index in range(0, 6000, 50):
            harness.draw_at(index)

        assert harness.widget.cached_chunks <= 4


class TestWhenThingsGoWrong:
    def test_a_lost_fix_keeps_the_last_map_on_screen(self):
        """Blanking the map for every dropped fix would make it flicker."""
        entries = ride(50)
        harness = Harness(entries)
        harness.draw_at(300)
        entries[301] = FakeEntry(entries[301].dt, None, None)

        canvas = harness.draw_at(301)

        assert centre(canvas)[:3] == (0, 0, 255)

    def test_no_fix_yet_draws_nothing_rather_than_failing(self):
        harness = Harness([FakeEntry(T0, None, None), *ride(50)[1:]])

        canvas = harness.draw_at(0)

        assert canvas.getbbox() is None

    def test_an_unreachable_tile_server_does_not_stop_the_render(self):
        """Losing the map background is better than losing an hour's render."""
        canvas = Harness(ride(50), renderer=FakeRenderer(fail=True)).draw_at(300)

        assert centre(canvas)[:3] == (0, 0, 255)

    def test_an_unreachable_tile_server_is_asked_once_per_chunk(self):
        """Each retry would stall a frame on a network timeout."""
        harness = Harness(ride(30), renderer=FakeRenderer(fail=True))

        for index in range(200, 260):
            harness.draw_at(index)

        assert len(harness.renderer.calls) == len(set(harness.renderer.calls))
