"""Reverse geocoding of GPS positions to settlement names.

Two interchangeable backends sit behind one interface: Nominatim (online,
accurate administrative containment) and a bundled GeoNames dataset (offline,
nearest settlement). Results are cached on disk so repeat renders cost nothing.

Place data (c) OpenStreetMap contributors (ODbL 1.0) and GeoNames (CC-BY 4.0).
"""

from dataclasses import dataclass
from enum import Enum


class Backend(str, Enum):
    """Which source answered a lookup.

    Tracked because refinement must never compare names from different backends:
    they word things differently, which would look like a boundary that is not there.
    """

    NOMINATIM = "nominatim"
    CITIES = "cities"


# Smallest settlement first, then widening outward.
# "hamlet" is deliberately absent: a person says "Giethoorn", not "Klooster".
_WIDENING_ORDER = ("village", "town", "city", "municipality", "county", "state", "country")


@dataclass(frozen=True)
class PlaceName:
    """A resolved administrative hierarchy.

    Stored unflattened so the cache stays reusable regardless of display
    settings - widening is applied at draw time, not at resolve time.
    """

    village: str | None = None
    town: str | None = None
    city: str | None = None
    municipality: str | None = None
    county: str | None = None
    state: str | None = None
    country: str | None = None

    def display_name(self) -> str:
        """Smallest available name, widening outward. Never None."""
        for field_name in _WIDENING_ORDER:
            value = getattr(self, field_name)
            if value:
                return value
        return ""
