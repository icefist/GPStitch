"""Choosing the map's zoom from how fast you are going.

Slow is a village street and wants to be close; fast is a main road and wants
the next few kilometres. The speed comes from the GPS track itself, averaged
over a few seconds and interpolated between fixes, so the zoom moves smoothly
from frame to frame rather than jumping once per GPS sample.
"""

import math
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from gpstitch.services.speed_zoom import (
    RouteIndex,
    SpeedTrack,
    chunks_covering,
    view_for,
    world_lonlat,
    world_px,
    zoom_for_speed,
)

T0 = datetime(2026, 9, 15, 17, 0, 0, tzinfo=UTC)
LAT, LON = 53.2194, 6.5665  # Groningen

# Metres per degree of latitude - near enough constant, and lets a test lay a
# track out at an exact speed by moving north.
M_PER_DEG_LAT = 111_195.0


def northward(kph_by_second, start=T0):
    """A track heading north at the given speed for each one-second step."""
    samples = [(start, LAT, LON)]
    lat = LAT
    for i, kph in enumerate(kph_by_second, start=1):
        lat += (kph / 3.6) / M_PER_DEG_LAT
        samples.append((start + timedelta(seconds=i), lat, LON))
    return samples


ZOOM = {"zoom_slow": 16.0, "zoom_fast": 12.0, "slow_kph": 20.0, "fast_kph": 110.0}


class TestZoomForSpeed:
    def test_at_or_below_the_slow_speed_it_is_fully_zoomed_in(self):
        assert zoom_for_speed(20, **ZOOM) == 16.0
        assert zoom_for_speed(3, **ZOOM) == 16.0

    def test_standing_still_is_fully_zoomed_in(self):
        assert zoom_for_speed(0, **ZOOM) == 16.0

    def test_at_or_above_the_fast_speed_it_is_fully_zoomed_out(self):
        assert zoom_for_speed(110, **ZOOM) == 12.0
        assert zoom_for_speed(180, **ZOOM) == 12.0

    def test_each_doubling_of_speed_zooms_out_by_the_same_amount(self):
        """Zoom levels are logarithmic, so speed is too - otherwise everything
        interesting happens in the first few km/h and the rest barely moves."""
        between = {"zoom_slow": 16.0, "zoom_fast": 12.0, "slow_kph": 10.0, "fast_kph": 160.0}

        steps = [zoom_for_speed(k, **between) for k in (10, 20, 40, 80, 160)]

        assert [round(a - b, 6) for a, b in pairwise(steps)] == [1.0, 1.0, 1.0, 1.0]

    def test_faster_never_means_closer(self):
        zooms = [zoom_for_speed(k, **ZOOM) for k in range(0, 150, 5)]

        assert zooms == sorted(zooms, reverse=True)


class TestSpeedTrack:
    def test_a_steady_speed_reads_as_that_speed(self):
        track = SpeedTrack(northward([36] * 20), window_s=4)

        assert track.kph_at(T0 + timedelta(seconds=10)) == pytest.approx(36, rel=1e-3)

    def test_it_reads_between_gps_fixes(self):
        """The overlay draws ten frames per second; GPS arrives once."""
        track = SpeedTrack(northward([36] * 20), window_s=4)

        assert track.kph_at(T0 + timedelta(seconds=10, milliseconds=350)) == pytest.approx(36, rel=1e-3)

    def test_the_reading_changes_smoothly_through_a_change_of_speed(self):
        """A step from 20 to 100 km/h must not reach the map as a step."""
        track = SpeedTrack(northward([20] * 10 + [100] * 10), window_s=4)

        readings = [track.kph_at(T0 + timedelta(seconds=6 + i / 10)) for i in range(80)]
        largest_jump = max(abs(b - a) for a, b in pairwise(readings))

        assert readings[0] == pytest.approx(20, rel=1e-3)
        assert readings[-1] == pytest.approx(100, rel=1e-3)
        assert largest_jump < 5

    def test_a_single_bad_fix_barely_registers(self):
        """One fix thrown 50m sideways would otherwise zoom the map out and back.

        Averaging the distance travelled would not help: the detour out and back
        is a hundred metres of real-looking travel. Measuring where you got to
        across the window, not how far you wandered getting there, ignores it.
        """
        samples = northward([30] * 20)
        when, lat, lon = samples[10]
        samples[10] = (when, lat, lon + 50 / (M_PER_DEG_LAT * math.cos(math.radians(LAT))))

        raw = SpeedTrack(samples, window_s=0).kph_at(T0 + timedelta(seconds=10, milliseconds=500))
        track = SpeedTrack(samples, window_s=6)
        across = [track.kph_at(T0 + timedelta(seconds=5 + i / 10)) for i in range(110)]

        assert raw > 150
        assert track.kph_at(T0 + timedelta(seconds=10, milliseconds=500)) == pytest.approx(30, rel=0.05)
        assert max(across) < 48

    def test_before_and_after_the_track_it_holds_the_nearest_reading(self):
        track = SpeedTrack(northward([36] * 20), window_s=4)

        assert track.kph_at(T0 - timedelta(seconds=30)) == pytest.approx(36, rel=1e-2)
        assert track.kph_at(T0 + timedelta(hours=1)) == pytest.approx(36, rel=1e-2)

    def test_repeated_timestamps_do_not_divide_by_zero(self):
        samples = northward([36] * 10)
        samples.insert(5, samples[5])

        assert SpeedTrack(samples, window_s=4).kph_at(T0 + timedelta(seconds=5)) == pytest.approx(36, rel=1e-2)

    def test_too_little_track_gives_no_reading(self):
        assert SpeedTrack([], window_s=4).kph_at(T0) is None
        assert SpeedTrack([(T0, LAT, LON)], window_s=4).kph_at(T0) is None


class TestWebMercator:
    def test_the_origin_of_the_world_is_the_centre_of_the_single_zoom_zero_tile(self):
        assert world_px(0.0, 0.0, 0) == pytest.approx((128.0, 128.0))

    def test_a_position_survives_the_round_trip(self):
        x, y = world_px(LON, LAT, 15)

        assert world_lonlat(x, y, 15) == pytest.approx((LON, LAT), abs=1e-9)

    def test_each_zoom_level_doubles_the_pixels(self):
        x14, y14 = world_px(LON, LAT, 14)
        x15, y15 = world_px(LON, LAT, 15)

        assert (x15, y15) == pytest.approx((x14 * 2, y14 * 2))


class TestView:
    def test_a_whole_zoom_is_drawn_at_its_own_tiles(self):
        view = view_for(LON, LAT, 15.0, size=400)

        assert view.tile_zoom == 15
        assert view.factor == pytest.approx(1.0)
        assert view.extent == pytest.approx(400)

    def test_a_fractional_zoom_draws_the_next_level_up_and_shrinks_it(self):
        """Shrinking sharper tiles stays crisp; enlarging coarser ones blurs."""
        view = view_for(LON, LAT, 14.5, size=400)

        assert view.tile_zoom == 15
        assert view.factor == pytest.approx(math.sqrt(2))
        assert view.extent == pytest.approx(400 * math.sqrt(2))

    def test_the_position_is_at_the_centre(self):
        view = view_for(LON, LAT, 14.3, size=400)

        assert view.to_output(*world_px(LON, LAT, view.tile_zoom)) == pytest.approx((200, 200))

    def test_floating_point_noise_does_not_jump_a_whole_level(self):
        assert view_for(LON, LAT, 15.0000000001, size=400).tile_zoom == 15


class TestChunksCovering:
    def test_a_view_inside_one_chunk_needs_only_that_chunk(self):
        assert chunks_covering(10.0, 10.0, 100.0, chunk=512) == [(0, 0)]

    def test_a_view_across_a_corner_needs_all_four(self):
        assert sorted(chunks_covering(500.0, 500.0, 100.0, chunk=512)) == [(0, 0), (0, 1), (1, 0), (1, 1)]

    def test_a_view_ending_exactly_on_an_edge_does_not_take_the_next_chunk(self):
        assert chunks_covering(0.0, 0.0, 512.0, chunk=512) == [(0, 0)]


class TestRouteIndex:
    def test_only_the_route_near_the_view_comes_back(self):
        """Drawing a three-hour ride every frame to show 500 metres of it is
        where the time would go."""
        near = [(x, 100.0) for x in range(0, 1000, 10)]
        far = [(x, 50_000.0) for x in range(1000, 0, -10)]
        index = RouteIndex(near + far, cell=256)

        runs = index.runs_in(0, 0, 900, 900)
        drawn = [p for run in runs for p in run]

        # The segment joining the two stretches crosses the view, so its far end
        # has to come back to be drawn - but nothing of the far stretch beyond it.
        assert drawn
        assert len([p for p in drawn if p[1] >= 1000]) == 1

    def test_a_run_through_the_view_comes_back_unbroken(self):
        index = RouteIndex([(x, 300.0) for x in range(0, 3000, 10)], cell=256)

        runs = index.runs_in(1000, 0, 1500, 600)

        assert len(runs) == 1
        assert runs[0][0][0] <= 1000 and runs[0][-1][0] >= 1500

    def test_points_closer_than_a_pixel_are_dropped(self):
        """At low zoom a ten-a-second track puts dozens of points on each pixel."""
        index = RouteIndex([(i * 0.01, 0.0) for i in range(1000)], cell=256)

        assert index.point_count < 20

    def test_an_empty_route_draws_nothing(self):
        assert RouteIndex([], cell=256).runs_in(0, 0, 100, 100) == []
