"""Tests for place name resolution."""

from gpstitch.services.place_resolver import Backend, PlaceName


class TestPlaceNameWidening:
    """display_name() walks smallest-settlement-first, then widens (spec D2, D4)."""

    def test_prefers_village_over_larger_units(self):
        p = PlaceName(village="Giethoorn", municipality="Steenwijkerland", state="Overijssel", country="Netherlands")
        assert p.display_name() == "Giethoorn"

    def test_prefers_city_when_no_village_or_town(self):
        p = PlaceName(city="Bath", county="Somerset", country="United Kingdom")
        assert p.display_name() == "Bath"

    def test_widens_to_county_when_no_settlement(self):
        """Khibiny mountains: the real Nominatim response has no settlement at all."""
        p = PlaceName(county="Kirovsk Urban Okrug", state="Murmansk Oblast", country="Russia")
        assert p.display_name() == "Kirovsk Urban Okrug"

    def test_widens_all_the_way_to_country(self):
        assert PlaceName(country="Russia").display_name() == "Russia"

    def test_empty_place_returns_empty_string_not_none(self):
        """C6: CachingText raises ValueError on None."""
        assert PlaceName().display_name() == ""

    def test_hamlet_is_not_a_field(self):
        """D4: hamlet is deliberately excluded - 'Giethoorn', not 'Klooster'."""
        assert not hasattr(PlaceName(), "hamlet")


class TestBackendEnum:
    def test_members(self):
        assert Backend.NOMINATIM.value == "nominatim"
        assert Backend.CITIES.value == "cities"
