"""Tests for progressive, disagreement-gated place sampling."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from gpstitch.services.place_resolver import Backend, PlaceName
from gpstitch.services.place_track import PlaceSample, PlaceTrack, build_place_track

T0 = datetime(2026, 9, 4, 12, 0, 0)


@dataclass
class FakePoint:
    lat: float
    lon: float


class FakeQuantity:
    """Stands in for a pint Quantity, which carries .magnitude."""

    def __init__(self, magnitude):
        self.magnitude = magnitude


class FakeEntry:
    """Stands in for a gopro_overlay Entry: dt, point, and cumulative distance."""

    def __init__(self, dt, lat, lon, codo_m):
        self.dt = dt
        self.point = FakePoint(lat, lon)
        self.codo = FakeQuantity(codo_m) if codo_m is not None else None


class FakeFrameMeta:
    def __init__(self, entries):
        self.frames = dict(enumerate(entries))


def _straight_track(n=101, total_m=50_000):
    """n entries evenly spaced along a line, with cumulative distance."""
    return FakeFrameMeta(
        [
            FakeEntry(T0 + timedelta(seconds=i * 10), 51.0 + i * 0.001, -2.0, total_m * i / max(1, n - 1))
            for i in range(n)
        ]
    )


class CountingResolver:
    """Returns a name based on latitude bands, and counts calls."""

    def __init__(self, boundaries=(), backend=Backend.NOMINATIM):
        self.calls = 0
        self.boundaries = boundaries
        self.backend = backend

    def resolve(self, lat, lon, lang):
        self.calls += 1
        idx = sum(1 for b in self.boundaries if lat >= b)
        return PlaceName(city=f"Town{idx}"), self.backend


class TestInitialCoverage:
    def test_level_zero_covers_the_whole_track(self):
        r = CountingResolver()
        track = build_place_track(_straight_track(), r, "en", 500, 11, 400)
        assert r.calls == 11
        assert track.samples[0].dt == T0
        assert track.samples[-1].dt == T0 + timedelta(seconds=1000)

    def test_uniform_area_needs_no_refinement(self):
        """All samples agree, so nothing is bisected."""
        r = CountingResolver()
        build_place_track(_straight_track(), r, "en", 500, 11, 400)
        assert r.calls == 11


class TestDisagreementGating:
    def test_one_boundary_triggers_refinement(self):
        r = CountingResolver(boundaries=(51.05,))
        build_place_track(_straight_track(), r, "en", 500, 11, 400)
        assert r.calls > 11

    def test_refinement_stays_far_below_uniform_sampling(self):
        """50km at 500m would be ~100 calls; gating should cost far less."""
        r = CountingResolver(boundaries=(51.05,))
        build_place_track(_straight_track(), r, "en", 500, 11, 400)
        assert r.calls < 60

    def test_respects_the_call_budget(self):
        """An all-different track must not run away."""
        r = CountingResolver(boundaries=tuple(51.0 + i * 0.002 for i in range(50)))
        build_place_track(_straight_track(), r, "en", 500, 11, 20)
        assert r.calls <= 20

    def test_does_not_compare_across_backends(self):
        """A backend switch stops refinement rather than inventing a boundary."""

        class SwitchingResolver:
            def __init__(self):
                self.calls = 0

            def resolve(self, lat, lon, lang):
                self.calls += 1
                if self.calls <= 11:
                    return PlaceName(city="Same"), Backend.NOMINATIM
                return PlaceName(city="DifferentWording"), Backend.CITIES

        r = SwitchingResolver()
        build_place_track(_straight_track(), r, "en", 500, 11, 400)
        assert r.calls == 11


class TestLookup:
    def test_returns_name_at_an_exact_sample(self):
        track = PlaceTrack([PlaceSample(T0, PlaceName(city="Bath"), Backend.NOMINATIM)])
        assert track.at(T0) == "Bath"

    def test_holds_the_last_name_between_samples(self):
        track = PlaceTrack(
            [
                PlaceSample(T0, PlaceName(city="Bath"), Backend.NOMINATIM),
                PlaceSample(T0 + timedelta(minutes=10), PlaceName(city="Bristol"), Backend.NOMINATIM),
            ]
        )
        assert track.at(T0 + timedelta(minutes=5)) == "Bath"

    def test_before_the_first_sample_uses_the_first_name(self):
        track = PlaceTrack([PlaceSample(T0, PlaceName(city="Bath"), Backend.NOMINATIM)])
        assert track.at(T0 - timedelta(hours=1)) == "Bath"

    def test_after_the_last_sample_uses_the_last_name(self):
        track = PlaceTrack([PlaceSample(T0, PlaceName(city="Bath"), Backend.NOMINATIM)])
        assert track.at(T0 + timedelta(hours=1)) == "Bath"

    def test_empty_track_returns_empty_string(self):
        assert PlaceTrack([]).at(T0) == ""

    def test_none_place_returns_empty_string(self):
        """Open ocean resolves to nothing at all."""
        assert PlaceTrack([PlaceSample(T0, None, Backend.CITIES)]).at(T0) == ""

    def test_none_dt_returns_empty_string(self):
        """entry() is None before the first draw."""
        assert PlaceTrack([PlaceSample(T0, PlaceName(city="Bath"), Backend.NOMINATIM)]).at(None) == ""


class TestDegenerateInputs:
    def test_empty_framemeta_yields_empty_track(self):
        r = CountingResolver()
        assert build_place_track(FakeFrameMeta([]), r, "en", 500, 11, 400).samples == []
        assert r.calls == 0

    def test_single_entry_needs_one_lookup(self):
        r = CountingResolver()
        track = build_place_track(_straight_track(n=1, total_m=0), r, "en", 500, 11, 400)
        assert r.calls == 1
        assert len(track.samples) == 1

    def test_track_shorter_than_target_stops_at_level_zero(self):
        r = CountingResolver(boundaries=(51.0005,))
        build_place_track(_straight_track(n=11, total_m=200), r, "en", 500, 11, 400)
        assert r.calls == 11

    def test_missing_codo_falls_back_to_time_subdivision(self):
        """codo is None when dist is missing."""
        fm = FakeFrameMeta(
            [FakeEntry(T0 + timedelta(seconds=i * 10), 51.0 + i * 0.001, -2.0, None) for i in range(101)]
        )
        r = CountingResolver()
        track = build_place_track(fm, r, "en", 500, 11, 400)
        assert r.calls == 11
        assert len(track.samples) == 11

    def test_entries_without_a_point_are_skipped(self):
        """No GPS fix on some entries."""
        entries = [FakeEntry(T0 + timedelta(seconds=i * 10), 51.0 + i * 0.001, -2.0, i * 500.0) for i in range(11)]
        entries[3].point = None
        r = CountingResolver()
        track = build_place_track(FakeFrameMeta(entries), r, "en", 500, 11, 400)
        assert all(s.place is not None for s in track.samples)

    def test_resolver_exception_does_not_abort_the_build(self):
        """A failed lookup must never fail a render."""

        class FlakyResolver:
            def __init__(self):
                self.calls = 0

            def resolve(self, lat, lon, lang):
                self.calls += 1
                if self.calls == 3:
                    raise RuntimeError("transient")
                return PlaceName(city="Bath"), Backend.NOMINATIM

        track = build_place_track(_straight_track(), FlakyResolver(), "en", 500, 11, 400)
        assert len(track.samples) == 10  # 11 attempted, 1 failed
        assert track.at(T0) == "Bath"


class TestSettlementLookup:
    """Telling a village/town/city from a county or country.

    The map zooms in when you are inside a settlement, and `at()` returns only a
    display string - which cannot distinguish "Groningen the city" from
    "Groningen the province" once it has been flattened.
    """

    def _track(self, place):
        from datetime import UTC, datetime

        from gpstitch.services.place_resolver import Backend
        from gpstitch.services.place_track import PlaceSample, PlaceTrack

        return PlaceTrack(
            [PlaceSample(dt=datetime(2026, 9, 13, 14, 0, tzinfo=UTC), place=place, backend=Backend.CITIES)]
        )

    def test_a_village_is_a_settlement(self):
        from datetime import UTC, datetime

        from gpstitch.services.place_resolver import PlaceName

        track = self._track(PlaceName(village="Giethoorn", country="Netherlands"))

        assert track.in_settlement(datetime(2026, 9, 13, 14, 1, tzinfo=UTC)) is True

    def test_a_town_is_a_settlement(self):
        from datetime import UTC, datetime

        from gpstitch.services.place_resolver import PlaceName

        track = self._track(PlaceName(town="Bath", country="England"))

        assert track.in_settlement(datetime(2026, 9, 13, 14, 1, tzinfo=UTC)) is True

    def test_a_city_is_a_settlement(self):
        from datetime import UTC, datetime

        from gpstitch.services.place_resolver import PlaceName

        track = self._track(PlaceName(city="Groningen", country="Netherlands"))

        assert track.in_settlement(datetime(2026, 9, 13, 14, 1, tzinfo=UTC)) is True

    def test_open_country_is_not_a_settlement(self):
        """Between places only the wider administrative names resolve."""
        from datetime import UTC, datetime

        from gpstitch.services.place_resolver import PlaceName

        track = self._track(PlaceName(county="Drenthe", country="Netherlands"))

        assert track.in_settlement(datetime(2026, 9, 13, 14, 1, tzinfo=UTC)) is False

    def test_an_unresolved_point_is_not_a_settlement(self):
        from datetime import UTC, datetime

        track = self._track(None)

        assert track.in_settlement(datetime(2026, 9, 13, 14, 1, tzinfo=UTC)) is False

    def test_no_samples_is_not_a_settlement(self):
        from datetime import UTC, datetime

        from gpstitch.services.place_track import PlaceTrack

        assert PlaceTrack([]).in_settlement(datetime(2026, 9, 13, 14, 1, tzinfo=UTC)) is False

    def test_no_time_is_not_a_settlement(self):
        from gpstitch.services.place_resolver import PlaceName

        track = self._track(PlaceName(city="Groningen"))

        assert track.in_settlement(None) is False
