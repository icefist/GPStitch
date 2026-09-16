"""Deciding what the single map should be framed on, moment to moment.

Between places it shows the whole ride. Inside a village, town or city it frames
the part of the route that runs through that place, so a position within the
town is actually readable.

Only the choice and the framing live here - the drawing is the widget's job.
"""

from datetime import UTC, datetime, timedelta

import pytest

from gpstitch.services.map_focus import MapFocus, route_bounds

START = datetime(2026, 9, 13, 14, 0, tzinfo=UTC)


class Pt:
    def __init__(self, lat, lon):
        self.lat = lat
        self.lon = lon


class Entry:
    def __init__(self, dt, lat, lon):
        self.dt = dt
        self.point = Pt(lat, lon)


class FakeTrack:
    """Answers in_settlement/place_at from a list of (dt, name) in order."""

    def __init__(self, timeline):
        self.timeline = timeline

    def _name(self, dt):
        name = None
        for at, value in self.timeline:
            if dt is None or at > dt:
                break
            name = value
        return name

    def in_settlement(self, dt):
        return self._name(dt) is not None

    def place_at(self, dt):
        name = self._name(dt)
        return type("P", (), {"display_name": lambda self, n=name: n or ""})() if name else None

    def at(self, dt):
        return self._name(dt) or ""


def entries_along(count, start_lat=53.2, step=0.001):
    return [Entry(START + timedelta(seconds=i), start_lat + i * step, 6.5) for i in range(count)]


class TestRouteBounds:
    def test_the_bounds_span_every_point(self):
        bounds = route_bounds([Pt(53.0, 6.0), Pt(53.5, 6.5), Pt(53.2, 6.2)])

        assert bounds == (53.0, 6.0, 53.5, 6.5)

    def test_a_single_point_gives_a_degenerate_box(self):
        assert route_bounds([Pt(53.0, 6.0)]) == (53.0, 6.0, 53.0, 6.0)

    def test_no_points_gives_nothing(self):
        assert route_bounds([]) is None


class TestMapFocus:
    def test_open_road_shows_the_whole_route(self):
        focus = MapFocus(FakeTrack([]), dwell_s=0)

        assert focus.settlement_at(START) is None

    def test_inside_a_town_focuses_on_it(self):
        focus = MapFocus(FakeTrack([(START, "Zuidhorn")]), dwell_s=0)

        assert focus.settlement_at(START + timedelta(seconds=5)) == "Zuidhorn"

    def test_leaving_a_town_returns_to_the_whole_route(self):
        track = FakeTrack([(START, "Zuidhorn"), (START + timedelta(seconds=10), None)])
        focus = MapFocus(track, dwell_s=0)

        assert focus.settlement_at(START + timedelta(seconds=20)) is None

    def test_a_brief_exit_does_not_zoom_straight_back_out(self):
        """A road clipping a village edge would otherwise jump twice in a mile."""
        track = FakeTrack([(START, "Zuidhorn"), (START + timedelta(seconds=10), None)])
        focus = MapFocus(track, dwell_s=15)

        focus.settlement_at(START + timedelta(seconds=5))

        assert focus.settlement_at(START + timedelta(seconds=12)) == "Zuidhorn"

    def test_the_dwell_eventually_expires(self):
        track = FakeTrack([(START, "Zuidhorn"), (START + timedelta(seconds=10), None)])
        focus = MapFocus(track, dwell_s=15)

        focus.settlement_at(START + timedelta(seconds=5))

        assert focus.settlement_at(START + timedelta(seconds=40)) is None

    def test_entering_a_different_town_switches_at_once(self):
        """Dwell smooths leaving, not arriving somewhere new."""
        track = FakeTrack([(START, "Zuidhorn"), (START + timedelta(seconds=10), "Leek")])
        focus = MapFocus(track, dwell_s=60)

        focus.settlement_at(START + timedelta(seconds=5))

        assert focus.settlement_at(START + timedelta(seconds=12)) == "Leek"

    def test_no_time_shows_the_whole_route(self):
        focus = MapFocus(FakeTrack([(START, "Zuidhorn")]), dwell_s=0)

        assert focus.settlement_at(None) is None


class TestSettlementBounds:
    def test_only_the_points_inside_that_town_are_framed(self):
        """Framing the whole route would defeat the purpose."""
        entries = entries_along(10)
        track = FakeTrack([(START, None), (entries[4].dt, "Zuidhorn"), (entries[7].dt, None)])
        focus = MapFocus(track, dwell_s=0)

        bounds = focus.bounds_for(entries, "Zuidhorn")

        assert bounds == pytest.approx((entries[4].point.lat, 6.5, entries[6].point.lat, 6.5))

    def test_a_town_with_no_points_has_no_bounds(self):
        focus = MapFocus(FakeTrack([]), dwell_s=0)

        assert focus.bounds_for(entries_along(5), "Nowhere") is None

    def test_the_bounds_are_computed_once_per_town(self):
        """Recomputing per frame would scan the whole track every frame."""
        entries = entries_along(10)
        track = FakeTrack([(entries[0].dt, "Zuidhorn")])
        focus = MapFocus(track, dwell_s=0)

        first = focus.bounds_for(entries, "Zuidhorn")
        second = focus.bounds_for(entries, "Zuidhorn")

        assert first is second
