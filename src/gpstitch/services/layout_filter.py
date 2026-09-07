"""Drop layout components that need the GPS position to move.

Some DJI clips carry a stuck GPS fix - the same coordinates for the whole
recording, because the Bluetooth remote lost its link and kept re-reporting its
last known position. A map cannot pan and a place name cannot change on such a
track, so those components are removed rather than drawn dead.

gopro-overlay's own `--exclude` only matches components that carry a `name`
attribute, and templates written in the GPStitch editor have none, so the
filtering has to happen on the XML itself.
"""

import logging
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)


# Everything whose output is a function of where the track goes. The map variants
# have to be listed exhaustively - one missed name draws a frozen map.
POSITION_COMPONENT_TYPES = frozenset(
    {
        "journey_map",
        "moving_map",
        "moving_journey_map",
        "circuit_map",
        "cairo_circuit_map",
        "place",
        "compass",
        "compass_arrow",
        # A stale fix still reports a 3D lock, so this would falsely show "locked".
        "gps_lock_icon",
    }
)


def filter_layout_components(xml: str, drop_types) -> tuple[str, list[str], int]:
    """Remove `<component>` elements whose type is in `drop_types`.

    Components sit at any depth, since templates wrap them in `<translate>` for
    positioning, and an emptied wrapper is left in place because it draws
    nothing on its own.

    Returns:
        (filtered_xml, dropped_types, remaining_component_count)
    """
    root = ET.fromstring(xml)

    doomed = [
        (parent, child)
        for parent in root.iter()
        for child in list(parent)
        if child.tag == "component" and child.get("type") in drop_types
    ]
    for parent, child in doomed:
        parent.remove(child)

    dropped = [child.get("type") for _, child in doomed]
    remaining = sum(1 for element in root.iter() if element.tag == "component")
    return ET.tostring(root, encoding="unicode"), dropped, remaining
