"""Dropping layout components that need the GPS position to move.

Some DJI clips carry a stuck GPS fix - the same coordinates for the whole
recording, because the Bluetooth remote lost its link. A map cannot pan and a
place name cannot change on such a track, so those components are removed
rather than drawn dead.

gopro-overlay's own --exclude only matches components carrying a `name`
attribute, and templates written in the GPStitch editor have none, so the
filtering has to happen on the XML itself.
"""

from gpstitch.services.layout_filter import (
    POSITION_COMPONENT_TYPES,
    filter_layout_components,
)


def test_a_position_component_is_removed():
    xml = '<layout><component type="journey_map" x="1" /><component type="big_mph" x="2" /></layout>'

    filtered, dropped, remaining = filter_layout_components(xml, POSITION_COMPONENT_TYPES)

    assert dropped == ["journey_map"]
    assert remaining == 1
    assert "journey_map" not in filtered
    assert "big_mph" in filtered


def test_components_nested_in_a_translate_are_reached():
    """Templates wrap components in <translate> for positioning."""
    xml = (
        "<layout>"
        '<translate x="10" y="20"><component type="compass_arrow" size="256" /></translate>'
        '<translate x="30" y="40"><component type="text" /></translate>'
        "</layout>"
    )

    filtered, dropped, remaining = filter_layout_components(xml, POSITION_COMPONENT_TYPES)

    assert dropped == ["compass_arrow"]
    assert remaining == 1
    assert "compass_arrow" not in filtered


def test_every_position_component_is_reported():
    xml = (
        "<layout>"
        '<component type="journey_map" />'
        '<component type="place" />'
        '<translate x="1" y="2"><component type="compass_arrow" /></translate>'
        '<component type="gps_lock_icon" />'
        "</layout>"
    )

    _, dropped, remaining = filter_layout_components(xml, POSITION_COMPONENT_TYPES)

    assert sorted(dropped) == ["compass_arrow", "gps_lock_icon", "journey_map", "place"]
    assert remaining == 0


def test_a_layout_with_nothing_to_drop_is_unchanged():
    xml = '<layout><component type="big_mph" /><component type="altitude" /></layout>'

    filtered, dropped, remaining = filter_layout_components(xml, POSITION_COMPONENT_TYPES)

    assert dropped == []
    assert remaining == 2
    assert "big_mph" in filtered and "altitude" in filtered


def test_remaining_counts_only_components():
    """An emptied <translate> is inert, so it does not count as something to draw."""
    xml = '<layout><translate x="1" y="2"><component type="place" /></translate></layout>'

    _, _, remaining = filter_layout_components(xml, POSITION_COMPONENT_TYPES)

    assert remaining == 0


def test_maps_place_and_compass_are_all_covered():
    """The set has to name every map variant, or one slips through and draws dead."""
    for component_type in (
        "journey_map",
        "moving_map",
        "moving_journey_map",
        "circuit_map",
        "cairo_circuit_map",
        "place",
        "compass",
        "compass_arrow",
    ):
        assert component_type in POSITION_COMPONENT_TYPES, component_type


def test_unrelated_components_are_not_in_the_set():
    for component_type in ("big_mph", "altitude", "text", "chart", "datetime", "gradient_chart"):
        assert component_type not in POSITION_COMPONENT_TYPES, component_type
