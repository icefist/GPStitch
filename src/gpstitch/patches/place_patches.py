"""Patch adding a `place` component that renders the current settlement name.

The track cannot be built when patches are applied - gopro-dashboard.py loads
its data afterwards - so it is built lazily at widget-creation time, which
happens after the data is loaded and where `self.framemeta` is available.
"""

import contextlib
import logging
import threading
import weakref

logger = logging.getLogger(__name__)

# Memoised tracks, keyed on the framemeta OBJECT via weakref.
#
# id(framemeta) would be wrong: preview rebuilds and discards framemeta on every
# request, and CPython reuses freed addresses, so an id() key eventually returns
# the previous video's track. A WeakKeyDictionary keys on identity safely and
# evicts entries when the framemeta is collected.
_TRACKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

# Thread-local because preview runs on a ThreadPoolExecutor while a render may
# be in flight; a global flag would leak one context into the other.
_state = threading.local()


def _preview_active() -> bool:
    return getattr(_state, "preview", False)


@contextlib.contextmanager
def preview_budget():
    """Use the smaller preview lookup budget inside this block.

    Preview blocks on the network by design and runs on a two-worker executor,
    so an uncached long track would otherwise freeze the editor. Because every
    refinement level is complete in itself, the coarse answer preview gets is
    correct - just less precise at transitions.
    """
    previous = _preview_active()
    _state.preview = True
    try:
        yield
    finally:
        _state.preview = previous


def track_for(framemeta, lang: str, target_m: int, resolver=None, max_lookups: int | None = None):
    """Build (or reuse) the place track for this framemeta."""
    from gpstitch.config import settings
    from gpstitch.services.place_resolver import PlaceResolver
    from gpstitch.services.place_track import build_place_track

    per_framemeta = _TRACKS.get(framemeta)
    if per_framemeta is None:
        per_framemeta = {}
        _TRACKS[framemeta] = per_framemeta

    key = (lang, target_m)
    cached = per_framemeta.get(key)
    if cached is not None:
        return cached

    if max_lookups is not None:
        budget = max_lookups
    elif _preview_active():
        budget = settings.place_preview_max_lookups
    else:
        budget = settings.place_max_lookups

    track = build_place_track(
        framemeta,
        resolver if resolver is not None else PlaceResolver(),
        lang,
        target_m,
        settings.place_initial_samples,
        budget,
    )
    per_framemeta[key] = track
    return track


def patch_place_widget() -> None:
    """Add `create_place` to the gopro_overlay widget factory."""
    from gopro_overlay import layout_xml
    from gopro_overlay.layout_components import text
    from gopro_overlay.layout_xml import at, attrib, iattrib, rgbattr
    from gopro_overlay.layout_xml_attribute import allow_attributes

    if getattr(layout_xml, "_ts_place_patched", False):
        logger.debug("create_place already patched, skipping")
        return

    from gpstitch.config import settings

    @allow_attributes({"x", "y", "size", "align", "rgb", "outline", "outline_width", "direction", "lang", "cache"})
    def create_place(self, element, entry, resolver=None, **kwargs):
        lang = attrib(element, "lang", d="en")
        track = track_for(self.framemeta, lang, settings.place_target_metres, resolver=resolver)

        def value() -> str:
            """Always a str - CachingText raises ValueError on None."""
            current = entry()
            if current is None:
                return ""
            return track.at(getattr(current, "dt", None))

        return text(
            at=at(element),
            value=value,
            font=self._font(element, "size", d=16),
            align=attrib(element, "align", d="left"),
            direction=attrib(element, "direction", d="ltr"),
            fill=rgbattr(element, "rgb", d=(255, 255, 255)),
            stroke=rgbattr(element, "outline", d=(0, 0, 0)),
            stroke_width=iattrib(element, "outline_width", d=2),
        )

    layout_xml.Widgets.create_place = create_place
    layout_xml._ts_place_patched = True
    logger.debug("Patched Widgets with create_place")
