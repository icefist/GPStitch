"""Tests for the online Nominatim backend and its cross-process rate limiter."""

import time
from unittest.mock import MagicMock

import pytest

from gpstitch.services.place_resolver import (
    NominatimBackend,
    PlaceLookupError,
    RateLimiter,
)

# Trimmed from a real Nominatim jsonv2 response for Giethoorn.
GIETHOORN = {
    "addresstype": "hamlet",
    "address": {
        "hamlet": "Klooster",
        "village": "Giethoorn",
        "municipality": "Steenwijkerland",
        "state": "Overijssel",
        "country": "Netherlands",
        "postcode": "8355 AB",
        "country_code": "nl",
    },
}

# Real response for the Khibiny mountains: no settlement of any kind.
WILDERNESS = {
    "addresstype": "county",
    "address": {
        "county": "Kirovsk Urban Okrug",
        "state": "Murmansk Oblast",
        "country": "Russia",
        "country_code": "ru",
    },
}


def _session(payload, status=200):
    s = MagicMock()
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    s.get.return_value = r
    return s


class TestNominatimParsing:
    def test_maps_address_keys_onto_placename(self):
        got = NominatimBackend(session=_session(GIETHOORN), limiter=None).resolve(52.74, 6.07, "en")
        assert got.village == "Giethoorn"
        assert got.municipality == "Steenwijkerland"
        assert got.state == "Overijssel"
        assert got.country == "Netherlands"

    def test_ignores_hamlet_key(self):
        """hamlet is not part of the hierarchy - 'Giethoorn', not 'Klooster'."""
        got = NominatimBackend(session=_session(GIETHOORN), limiter=None).resolve(52.74, 6.07, "en")
        assert got.display_name() == "Giethoorn"

    def test_wilderness_yields_no_settlement_but_still_widens(self):
        got = NominatimBackend(session=_session(WILDERNESS), limiter=None).resolve(67.69, 33.59, "en")
        assert got.village is None and got.town is None and got.city is None
        assert got.display_name() == "Kirovsk Urban Okrug"

    def test_empty_address_yields_empty_display_name(self):
        """Open ocean: Nominatim returns no address at all."""
        got = NominatimBackend(session=_session({"address": {}}), limiter=None).resolve(0.0, -140.0, "en")
        assert got.display_name() == ""


class TestNominatimRequest:
    def test_sends_identifying_user_agent_and_language(self):
        s = _session(GIETHOORN)
        NominatimBackend(session=s, limiter=None).resolve(52.74, 6.07, "de")
        _, kwargs = s.get.call_args
        assert "GPStitch" in kwargs["headers"]["User-Agent"]
        assert kwargs["params"]["accept-language"] == "de"

    def test_requests_zoom_14_so_no_road_key_is_returned(self):
        """Street names are excluded structurally by zoom, not by filtering."""
        s = _session(GIETHOORN)
        NominatimBackend(session=s, limiter=None).resolve(52.74, 6.07, "en")
        assert s.get.call_args[1]["params"]["zoom"] == 14

    def test_network_error_raises_place_lookup_error(self):
        s = MagicMock()
        s.get.side_effect = OSError("connection refused")
        with pytest.raises(PlaceLookupError):
            NominatimBackend(session=s, limiter=None).resolve(52.74, 6.07, "en")

    def test_http_error_raises_place_lookup_error(self):
        s = MagicMock()
        r = MagicMock()
        r.raise_for_status.side_effect = OSError("429 Too Many Requests")
        s.get.return_value = r
        with pytest.raises(PlaceLookupError):
            NominatimBackend(session=s, limiter=None).resolve(52.74, 6.07, "en")


class TestRateLimiter:
    def test_first_call_does_not_wait(self, tmp_path):
        assert RateLimiter(tmp_path / "rl.sqlite", 1.0).wait() == pytest.approx(0.0, abs=0.05)

    def test_second_call_waits_the_interval(self, tmp_path):
        limiter = RateLimiter(tmp_path / "rl.sqlite", 0.3)
        limiter.wait()
        t0 = time.monotonic()
        limiter.wait()
        assert time.monotonic() - t0 >= 0.25

    def test_separate_instances_share_state_via_the_file(self, tmp_path):
        """Preview and render are different processes sharing one cap."""
        db = tmp_path / "rl.sqlite"
        RateLimiter(db, 0.3).wait()
        t0 = time.monotonic()
        RateLimiter(db, 0.3).wait()
        assert time.monotonic() - t0 >= 0.25
