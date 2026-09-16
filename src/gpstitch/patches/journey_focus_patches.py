"""Let `journey_map` frame the settlement you are in.

Drawn over a whole ride, a journey map makes a town an unreadable smudge: at
100km across, a position within a village is a few pixels. With `town_zoom` set,
the map frames the part of the route running through the current settlement, and
goes back to the whole ride between places.

Each framing is rendered once and cached, so the image is rebuilt only when you
enter or leave somewhere - not per frame. Within a view the marker moves over a
static image, exactly as the unmodified widget behaves.

Without the attribute the widget is left completely alone.
"""

import logging
import xml.etree.ElementTree as ET

from gpstitch.patches.place_patches import track_for

logger = logging.getLogger(__name__)

# How zoomed the settlement view may get. A hamlet whose route is 200m across
# would otherwise frame to street level, which is more zoom than is useful.
_DEFAULT_TOWN_ZOOM = 16

# Seconds to stay framed on a settlement after leaving it. A road clipping the
# edge of a village would otherwise zoom in and straight back out.
_DEFAULT_TOWN_DWELL_S = 15.0

# Matches the cap the unmodified journey map applies to its own whole-route view.
_MAX_ZOOM = 18

# Ours alone. The stock widget validates its attributes strictly, so these
# have to come off the element before it is handed anything.
_OUR_ATTRIBUTES = ("town_zoom", "town_dwell")


def patch_journey_map_focus() -> None:
    """Add `town_zoom` and `town_dwell` to the journey_map component."""
    import geotiler
    from gopro_overlay import layout_xml
    from gopro_overlay.journey import Journey
    from gopro_overlay.layout_xml import at, fattrib, iattrib
    from gopro_overlay.layout_xml_attribute import allow_attributes
    from gopro_overlay.rdp import rdp
    from gopro_overlay.widgets.map import MaybeRoundedBorder, draw_marker
    from gopro_overlay.widgets.widgets import Widget
    from PIL import ImageDraw

    if getattr(layout_xml, "_ts_journey_focus_patched", False):
        logger.debug("journey_map focus already patched, skipping")
        return

    from gpstitch.config import settings
    from gpstitch.services.map_focus import MapFocus, route_bounds

    original = layout_xml.Widgets.create_journey_map

    class FocusedJourneyMap(Widget):
        """A journey map that frames the settlement you are in."""

        def __init__(self, framemeta, entry, focus, renderer, privacy, size, border, town_zoom):
            self.framemeta = framemeta
            self.entry = entry
            self.focus = focus
            self.renderer = renderer
            self.privacy = privacy
            self.size = size
            self.border = border
            self.town_zoom = town_zoom
            self._views: dict[str | None, tuple | None] = {}
            self._entries = None

        def _ordered_entries(self):
            if self._entries is None:
                frames = self.framemeta.frames
                self._entries = [frames[k] for k in sorted(frames) if getattr(frames[k], "point", None) is not None]
            return self._entries

        def _journey_points(self):
            journey = Journey()
            self.framemeta.process(journey.accept)
            return [p for p in journey.locations if not self.privacy.encloses(p)]

        def _build_view(self, settlement: str | None):
            """Render one framing: the whole route, or one settlement's part of it."""
            points = self._journey_points()
            if not points:
                return None

            if settlement is None:
                bounds = route_bounds(points)
                cap = _MAX_ZOOM
            else:
                bounds = self.focus.bounds_for(self._ordered_entries(), settlement)
                cap = self.town_zoom
                if bounds is None:
                    return None

            min_lat, min_lon, max_lat, max_lon = bounds
            if min_lat == max_lat and min_lon == max_lon:
                # A degenerate box gives geotiler nothing to fit; nudge it open.
                min_lat, max_lat = min_lat - 0.001, max_lat + 0.001
                min_lon, max_lon = min_lon - 0.001, max_lon + 0.001

            backing = geotiler.Map(
                extent=(min_lon, min_lat, max_lon, max_lat),
                size=(self.size, self.size),
            )
            if backing.zoom > cap:
                backing.zoom = cap

            image = self.renderer(backing)
            plots = rdp([backing.rev_geocode((p.lon, p.lat)) for p in points], epsilon=1)
            if len(plots) > 1:
                ImageDraw.Draw(image).line(plots, fill=(255, 0, 0), width=4)

            logger.debug("Built journey view for %s at zoom %s", settlement or "the whole route", backing.zoom)
            return backing, self.border.rounded(image)

        def _view_for(self, settlement: str | None):
            if settlement not in self._views:
                self._views[settlement] = self._build_view(settlement)
            view = self._views[settlement]
            if view is None and settlement is not None:
                # Nothing to frame there - fall back to the whole route rather
                # than drawing nothing at all.
                return self._view_for(None)
            return view

        def draw(self, image, draw):
            current = self.entry()
            dt = getattr(current, "dt", None)
            view = self._view_for(self.focus.settlement_at(dt))
            if view is None:
                return

            backing, base = view
            location = getattr(current, "point", None)
            frame = base.copy()
            if location is not None and location.lat is not None and location.lon is not None:
                draw_marker(ImageDraw.Draw(frame), backing.rev_geocode((location.lon, location.lat)), 6)
            image.alpha_composite(frame, self.at.tuple())

    @allow_attributes({"x", "y", "size", "corner_radius", "opacity", "town_zoom", "town_dwell"})
    def create_journey_map(self, element, entry, **kwargs):
        town_zoom = iattrib(element, "town_zoom", d=0, r=range(0, _MAX_ZOOM + 1))
        if not town_zoom:
            # Absent, or switched off: the stock widget, untouched. Its own
            # attribute check would reject ours, so they are stripped first.
            stock = ET.Element(
                element.tag,
                {k: v for k, v in element.attrib.items() if k not in _OUR_ATTRIBUTES},
            )
            stock.extend(list(element))
            return original(self, stock, entry, **kwargs)

        size = iattrib(element, "size", d=256)
        widget = FocusedJourneyMap(
            framemeta=self.framemeta,
            entry=entry,
            focus=MapFocus(
                track_for(self.framemeta, "en", settings.place_target_metres),
                dwell_s=fattrib(element, "town_dwell", d=_DEFAULT_TOWN_DWELL_S),
            ),
            renderer=self.renderer,
            privacy=self.privacy,
            size=size,
            border=MaybeRoundedBorder(
                size=size,
                corner_radius=iattrib(element, "corner_radius", d=0),
                opacity=fattrib(element, "opacity", d=0.7),
            ),
            town_zoom=town_zoom or _DEFAULT_TOWN_ZOOM,
        )
        widget.at = at(element)
        return widget

    layout_xml.Widgets.create_journey_map = create_journey_map
    layout_xml._ts_journey_focus_patched = True
    logger.debug("Patched journey_map with settlement focus")
