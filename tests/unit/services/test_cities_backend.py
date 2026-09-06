"""Tests for the offline GeoNames backend."""

import gzip

import pytest

from gpstitch.services.place_resolver import CitiesBackend


@pytest.fixture
def tiny_data_dir(tmp_path):
    """A 3-settlement dataset. Never load the real 235k-row file in tests."""
    with gzip.open(tmp_path / "cities.tsv.gz", "wt", encoding="utf-8") as f:
        # name, lat, lon, population, cc, admin1, admin2
        f.write("Giethoorn\t52.74000\t6.07917\t2130\tNL\t15\t1708\n")
        f.write("Steenwijk\t52.78889\t6.11944\t17000\tNL\t15\t1708\n")
        f.write("Bath\t51.37500\t-2.36667\t94782\tGB\tENG\tSOM\n")
        f.write("Bristol\t51.45523\t-2.59665\t617280\tGB\tENG\tBST\n")
    (tmp_path / "admin1.tsv").write_text("NL.15\tOverijssel\nGB.ENG\tEngland\n", encoding="utf-8")
    with gzip.open(tmp_path / "admin2.tsv.gz", "wt", encoding="utf-8") as f:
        f.write("NL.15.1708\tGemeente Steenwijkerland\nGB.ENG.SOM\tSomerset\nGB.ENG.BST\tBristol\n")
    (tmp_path / "countries.tsv").write_text("NL\tThe Netherlands\nGB\tUnited Kingdom\n", encoding="utf-8")
    return tmp_path


class TestCitiesBackend:
    def test_finds_nearest_settlement(self, tiny_data_dir):
        got = CitiesBackend(tiny_data_dir).resolve(52.7402, 6.0781, "en")
        assert got.display_name() == "Giethoorn"

    def test_small_population_lands_in_village_slot(self, tiny_data_dir):
        got = CitiesBackend(tiny_data_dir).resolve(52.7402, 6.0781, "en")
        assert got.village == "Giethoorn"
        assert got.city is None

    def test_mid_population_lands_in_town_slot(self, tiny_data_dir):
        """Population drives the slot; Bath (94,782) is a town under the 100k cut."""
        got = CitiesBackend(tiny_data_dir).resolve(51.3750, -2.36667, "en")
        assert got.town == "Bath"
        assert got.village is None and got.city is None

    def test_large_population_lands_in_city_slot(self, tiny_data_dir):
        got = CitiesBackend(tiny_data_dir).resolve(51.45523, -2.59665, "en")
        assert got.city == "Bristol"
        assert got.village is None and got.town is None

    def test_slot_choice_does_not_change_the_displayed_name(self, tiny_data_dir):
        """village/town/city are all checked before widening, so display is stable."""
        backend = CitiesBackend(tiny_data_dir)
        assert backend.resolve(51.3750, -2.36667, "en").display_name() == "Bath"
        assert backend.resolve(51.45523, -2.59665, "en").display_name() == "Bristol"

    def test_resolves_admin_codes_to_names(self, tiny_data_dir):
        """Offline reaches county, state and country - not municipality."""
        got = CitiesBackend(tiny_data_dir).resolve(52.7402, 6.0781, "en")
        assert got.county == "Gemeente Steenwijkerland"
        assert got.state == "Overijssel"
        assert got.country == "The Netherlands"
        assert got.municipality is None

    def test_picks_the_closer_of_two_nearby_settlements(self, tiny_data_dir):
        got = CitiesBackend(tiny_data_dir).resolve(52.7880, 6.1190, "en")
        assert got.display_name() == "Steenwijk"

    def test_returns_none_when_nothing_within_the_band(self, tiny_data_dir):
        """Mid-Pacific: no settlement anywhere near."""
        assert CitiesBackend(tiny_data_dir).resolve(-40.0, -140.0, "en") is None

    def test_missing_data_dir_returns_none_and_does_not_raise(self, tmp_path):
        assert CitiesBackend(tmp_path / "nope").resolve(52.0, 6.0, "en") is None

    def test_loading_is_lazy(self, tiny_data_dir):
        """Constructing must not read the dataset."""
        backend = CitiesBackend(tiny_data_dir)
        assert backend._rows is None
        backend.resolve(52.7402, 6.0781, "en")
        assert backend._rows is not None
