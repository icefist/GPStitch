"""journey_map gains an optional settlement focus.

Without `town_zoom` the widget must be the stock one, untouched - existing
templates far outnumber the one asking for this, and the stock widget is what
they were designed against.
"""

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import pytest

from gpstitch.patches.journey_focus_patches import patch_journey_map_focus

T0 = datetime(2026, 9, 13, 14, 0, 0)


class FakePoint:
    def __init__(self, lat, lon):
        self.lat, self.lon = lat, lon


class FakeEntry:
    def __init__(self, dt, lat, lon):
        self.dt, self.point, self.codo = dt, FakePoint(lat, lon), None


class FakeFrameMeta:
    def __init__(self, n=5):
        self.frames = {i: FakeEntry(T0 + timedelta(seconds=i), 53.2 + i * 0.001, 6.5) for i in range(n)}

    def process(self, fn):
        for key in sorted(self.frames):
            fn(self.frames[key])


class NoPrivacy:
    def encloses(self, point):
        return False


def _factory(framemeta):
    from gopro_overlay import layout_xml
    from PIL import ImageFont

    return layout_xml.Widgets(
        font=lambda size: ImageFont.load_default(),
        privacy=NoPrivacy(),
        renderer=lambda m: None,
        framemeta=framemeta,
        converters=None,
    )


def _element(**attrs):
    return ET.Element("component", {"type": "journey_map", **{k: str(v) for k, v in attrs.items()}})


@pytest.fixture(autouse=True)
def _patched(monkeypatch):
    """Apply the patch, and keep place resolution off the network."""
    from gopro_overlay import layout_xml

    from gpstitch.patches import journey_focus_patches

    monkeypatch.setattr(layout_xml, "_ts_journey_focus_patched", False, raising=False)
    monkeypatch.setattr(journey_focus_patches, "logger", journey_focus_patches.logger)
    patch_journey_map_focus()
    yield


class TestJourneyMapFocus:
    def test_without_the_attribute_the_stock_widget_is_used(self):
        from gopro_overlay.widgets.map import JourneyMap

        widget = _factory(FakeFrameMeta()).create_journey_map(_element(x=0, y=0, size=64), entry=lambda: None)

        assert isinstance(widget, JourneyMap)

    def test_with_the_attribute_the_focused_widget_is_used(self, monkeypatch):
        from gpstitch.patches import journey_focus_patches

        monkeypatch.setattr(journey_focus_patches, "track_for", lambda *a, **k: _StubTrack())

        widget = _factory(FakeFrameMeta()).create_journey_map(
            _element(x=0, y=0, size=64, town_zoom=15), entry=lambda: None
        )

        assert type(widget).__name__ == "FocusedJourneyMap"

    def test_a_zero_town_zoom_means_the_stock_widget(self):
        """So the attribute can be left in a template but switched off."""
        from gopro_overlay.widgets.map import JourneyMap

        widget = _factory(FakeFrameMeta()).create_journey_map(
            _element(x=0, y=0, size=64, town_zoom=0), entry=lambda: None
        )

        assert isinstance(widget, JourneyMap)

    def test_the_new_attributes_are_accepted(self, monkeypatch):
        """An attribute the factory does not allow is a hard error at load."""
        from gpstitch.patches import journey_focus_patches

        monkeypatch.setattr(journey_focus_patches, "track_for", lambda *a, **k: _StubTrack())

        _factory(FakeFrameMeta()).create_journey_map(
            _element(x=0, y=0, size=64, town_zoom=15, town_dwell=30, corner_radius=8, opacity=0.5),
            entry=lambda: None,
        )

    def test_the_dwell_reaches_the_focus(self, monkeypatch):
        from gpstitch.patches import journey_focus_patches

        monkeypatch.setattr(journey_focus_patches, "track_for", lambda *a, **k: _StubTrack())

        widget = _factory(FakeFrameMeta()).create_journey_map(
            _element(x=0, y=0, size=64, town_zoom=15, town_dwell=42), entry=lambda: None
        )

        assert widget.focus.dwell_s == 42


class TestSpeedZoom:
    """`zoom_slow` / `zoom_fast` make the map follow you, zoomed by your speed."""

    def test_setting_a_speed_zoom_gives_the_following_map(self):
        widget = _factory(FakeFrameMeta()).create_journey_map(
            _element(x=0, y=0, size=64, zoom_slow=16, zoom_fast=12), entry=lambda: None
        )

        assert type(widget).__name__ == "SpeedZoomJourneyMap"

    def test_every_speed_zoom_attribute_is_accepted(self):
        """An attribute the factory does not allow is a hard error at load."""
        widget = _factory(FakeFrameMeta()).create_journey_map(
            _element(x=0, y=0, size=64, zoom_slow=17, zoom_fast=11, slow_kph=15, fast_kph=130, zoom_smoothing=6),
            entry=lambda: None,
        )

        assert (widget.zoom_slow, widget.zoom_fast) == (17.0, 11.0)
        assert (widget.slow_kph, widget.fast_kph) == (15.0, 130.0)
        assert widget.smoothing_s == 6.0

    def test_unset_values_fall_back_to_defaults(self):
        widget = _factory(FakeFrameMeta()).create_journey_map(
            _element(x=0, y=0, size=64, zoom_slow=17), entry=lambda: None
        )

        assert widget.zoom_slow == 17.0
        assert widget.zoom_fast < widget.zoom_slow

    def test_a_slow_speed_above_the_fast_one_is_refused_at_load(self):
        """Caught when the template is read, not an hour into a render."""
        with pytest.raises(ValueError, match="slow_kph"):
            _factory(FakeFrameMeta()).create_journey_map(
                _element(x=0, y=0, size=64, zoom_slow=16, slow_kph=100, fast_kph=50), entry=lambda: None
            )

    def test_a_zoom_the_tile_servers_do_not_have_is_refused_at_load(self):
        with pytest.raises(ValueError, match="zoom_slow"):
            _factory(FakeFrameMeta()).create_journey_map(_element(x=0, y=0, size=64, zoom_slow=22), entry=lambda: None)

    def test_speed_zoom_takes_over_from_town_framing(self):
        """Slowing down through a village already zooms in - the two would fight."""
        widget = _factory(FakeFrameMeta()).create_journey_map(
            _element(x=0, y=0, size=64, zoom_slow=16, town_zoom=15), entry=lambda: None
        )

        assert type(widget).__name__ == "SpeedZoomJourneyMap"


class _StubTrack:
    def in_settlement(self, dt):
        return False

    def place_at(self, dt):
        return None

    def at(self, dt):
        return ""
