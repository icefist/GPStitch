"""Tests for resolver caching and backend fallback."""

from unittest.mock import MagicMock

from gpstitch.services.place_resolver import (
    Backend,
    PlaceLookupError,
    PlaceName,
    PlaceResolver,
)

BATH = PlaceName(city="Bath", county="Somerset", country="United Kingdom")
BATH_OFFLINE = PlaceName(town="Bath", county="Somerset", country="United Kingdom")


def _backend(result=None, error=None):
    b = MagicMock()
    if error:
        b.resolve.side_effect = error
    else:
        b.resolve.return_value = result
    return b


class TestBackendSelection:
    def test_uses_online_when_it_answers(self, tmp_path):
        online, offline = _backend(BATH), _backend(BATH_OFFLINE)
        place, backend = PlaceResolver(online, offline, tmp_path / "c.sqlite").resolve(51.37, -2.36, "en")
        assert place == BATH
        assert backend is Backend.NOMINATIM
        offline.resolve.assert_not_called()

    def test_falls_back_to_offline_on_lookup_error(self, tmp_path):
        online = _backend(error=PlaceLookupError("boom"))
        offline = _backend(BATH_OFFLINE)
        place, backend = PlaceResolver(online, offline, tmp_path / "c.sqlite").resolve(51.37, -2.36, "en")
        assert place == BATH_OFFLINE
        assert backend is Backend.CITIES

    def test_skips_network_entirely_when_disabled(self, tmp_path):
        online, offline = _backend(BATH), _backend(BATH_OFFLINE)
        r = PlaceResolver(online, offline, tmp_path / "c.sqlite", enable_network=False)
        _, backend = r.resolve(51.37, -2.36, "en")
        assert backend is Backend.CITIES
        online.resolve.assert_not_called()

    def test_both_failing_returns_none(self, tmp_path):
        online = _backend(error=PlaceLookupError("boom"))
        offline = _backend(None)
        place, _ = PlaceResolver(online, offline, tmp_path / "c.sqlite").resolve(0.0, -140.0, "en")
        assert place is None


class TestCaching:
    def test_second_identical_lookup_hits_cache(self, tmp_path):
        online = _backend(BATH)
        r = PlaceResolver(online, _backend(None), tmp_path / "c.sqlite")
        r.resolve(51.37, -2.36, "en")
        r.resolve(51.37, -2.36, "en")
        assert online.resolve.call_count == 1

    def test_nearby_points_share_a_cache_bucket(self, tmp_path):
        """Keys round to 3dp (~110m), so a stationary rider reuses one entry."""
        online = _backend(BATH)
        r = PlaceResolver(online, _backend(None), tmp_path / "c.sqlite")
        r.resolve(51.370001, -2.360001, "en")
        r.resolve(51.370002, -2.360002, "en")
        assert online.resolve.call_count == 1

    def test_different_language_is_a_different_key(self, tmp_path):
        online = _backend(BATH)
        r = PlaceResolver(online, _backend(None), tmp_path / "c.sqlite")
        r.resolve(51.37, -2.36, "en")
        r.resolve(51.37, -2.36, "de")
        assert online.resolve.call_count == 2

    def test_cache_survives_a_new_resolver_instance(self, tmp_path):
        cache = tmp_path / "c.sqlite"
        PlaceResolver(_backend(BATH), _backend(None), cache).resolve(51.37, -2.36, "en")
        online2 = _backend(BATH)
        PlaceResolver(online2, _backend(None), cache).resolve(51.37, -2.36, "en")
        online2.resolve.assert_not_called()

    def test_cached_result_remembers_which_backend_answered(self, tmp_path):
        """Refinement must not compare across backends, so this must survive caching."""
        cache = tmp_path / "c.sqlite"
        online = _backend(error=PlaceLookupError("down"))
        PlaceResolver(online, _backend(BATH_OFFLINE), cache).resolve(51.37, -2.36, "en")
        _, backend = PlaceResolver(_backend(BATH), _backend(None), cache).resolve(51.37, -2.36, "en")
        assert backend is Backend.CITIES

    def test_unwritable_cache_does_not_break_resolution(self, tmp_path):
        """A locked or unwritable cache must never fail a render."""
        bad = tmp_path / "wall" / "c.sqlite"
        bad.parent.mkdir()
        bad.parent.chmod(0o500)
        try:
            place, backend = PlaceResolver(_backend(BATH), _backend(None), bad).resolve(51.37, -2.36, "en")
            assert place == BATH
            assert backend is Backend.NOMINATIM
        finally:
            bad.parent.chmod(0o700)
