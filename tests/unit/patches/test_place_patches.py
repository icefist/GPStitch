"""Tests for the create_place widget patch."""

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from gpstitch.patches.place_patches import patch_place_widget, track_for
from gpstitch.services.place_resolver import Backend, PlaceName

T0 = datetime(2026, 9, 4, 12, 0, 0)


class FakePoint:
    def __init__(self, lat, lon):
        self.lat, self.lon = lat, lon


class FakeEntry:
    def __init__(self, dt, lat, lon):
        self.dt, self.point, self.codo = dt, FakePoint(lat, lon), None


class FakeFrameMeta:
    def __init__(self, n=5):
        self.frames = {i: FakeEntry(T0 + timedelta(seconds=i), 51.0, -2.0) for i in range(n)}


class StubResolver:
    def __init__(self, name="Bath"):
        self.name, self.calls = name, 0

    def resolve(self, lat, lon, lang):
        self.calls += 1
        return PlaceName(city=self.name), Backend.NOMINATIM


def _factory(framemeta):
    from gopro_overlay import layout_xml
    from PIL import ImageFont

    return layout_xml.Widgets(
        font=lambda size: ImageFont.load_default(),
        privacy=None,
        renderer=None,
        framemeta=framemeta,
        converters=None,
    )


class TestTrackMemoisation:
    def test_same_arguments_reuse_one_track(self):
        fm, r = FakeFrameMeta(), StubResolver()
        assert track_for(fm, "en", 500, resolver=r) is track_for(fm, "en", 500, resolver=r)

    def test_different_language_builds_a_separate_track(self):
        """Two place widgets with different lang must not collide."""
        fm = FakeFrameMeta()
        a = track_for(fm, "en", 500, resolver=StubResolver("Bath"))
        b = track_for(fm, "de", 500, resolver=StubResolver("Bad"))
        assert a is not b
        assert b.at(T0) == "Bad"

    def test_distinct_framemetas_do_not_share_a_track(self):
        """A fresh framemeta must never inherit the previous video's names."""
        a = track_for(FakeFrameMeta(), "en", 500, resolver=StubResolver("Bath"))
        b = track_for(FakeFrameMeta(), "en", 500, resolver=StubResolver("Bristol"))
        assert a.at(T0) == "Bath"
        assert b.at(T0) == "Bristol"

    def test_track_is_released_when_framemeta_is_collected(self):
        """Keyed weakly, so a long-running server does not accumulate tracks."""
        import gc

        from gpstitch.patches import place_patches

        fm = FakeFrameMeta()
        track_for(fm, "en", 500, resolver=StubResolver())
        assert len(place_patches._TRACKS) >= 1
        del fm
        gc.collect()
        assert len(place_patches._TRACKS) == 0


class TestCreatePlace:
    def test_patch_registers_create_place_on_the_factory(self):
        from gopro_overlay import layout_xml

        patch_place_widget()
        assert hasattr(layout_xml.Widgets, "create_place")

    def test_patch_is_idempotent(self):
        from gopro_overlay import layout_xml

        patch_place_widget()
        first = layout_xml.Widgets.create_place
        patch_place_widget()
        assert layout_xml.Widgets.create_place is first

    def test_widget_renders_the_resolved_name(self):
        patch_place_widget()
        fm = FakeFrameMeta()
        element = ET.fromstring('<component type="place" x="10" y="20" size="24" lang="en"/>')
        widget = _factory(fm).create_place(element, entry=lambda: fm.frames[0], resolver=StubResolver("Bath"))
        assert widget.value() == "Bath"

    def test_widget_returns_empty_string_when_entry_is_none(self):
        """entry() is None before the first draw; CachingText raises on None."""
        patch_place_widget()
        element = ET.fromstring('<component type="place"/>')
        widget = _factory(FakeFrameMeta()).create_place(element, entry=lambda: None, resolver=StubResolver())
        assert widget.value() == ""

    def test_unknown_attribute_is_rejected(self):
        """allow_attributes guards against typos in hand-written layouts."""
        import pytest

        patch_place_widget()
        element = ET.fromstring('<component type="place" colour="red"/>')
        with pytest.raises(Exception):  # noqa: B017 - library raises its own Defect type
            _factory(FakeFrameMeta()).create_place(element, entry=lambda: None, resolver=StubResolver())
