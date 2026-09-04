"""Reverse geocoding of GPS positions to settlement names.

Two interchangeable backends sit behind one interface: Nominatim (online,
accurate administrative containment) and a bundled GeoNames dataset (offline,
nearest settlement). Results are cached on disk so repeat renders cost nothing.

Place data (c) OpenStreetMap contributors (ODbL 1.0) and GeoNames (CC-BY 4.0).
"""

import bisect
import csv
import gzip
import logging
import math
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import requests

from gpstitch.config import settings

logger = logging.getLogger(__name__)


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


_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Population thresholds sorting a settlement into a hierarchy slot. GeoNames has
# no village/town/city distinction, so this is a heuristic - the exact cut points
# matter little, since the feature targets coarse accuracy.
_VILLAGE_MAX_POP = 10_000
_TOWN_MAX_POP = 100_000

# Latitude half-window scanned around a query. 0.5 deg is ~55km, wider than any
# gap between settlements at cities500 density, and keeps the scan to roughly
# 1,500 of 235,000 rows.
_BAND_DEGREES = 0.5

# Beyond this, treat the result as "nowhere near a settlement".
_MAX_MATCH_KM = 75.0


def _read_kv(path: Path, gz: bool) -> dict[str, str]:
    """Read a two-column key/name TSV into a dict. A missing file yields {}."""
    if not path.exists():
        return {}
    opener = gzip.open if gz else open
    out: dict[str, str] = {}
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2:
                out[parts[0]] = parts[1]
    return out


class CitiesBackend:
    """Offline nearest-settlement lookup over the bundled GeoNames dataset.

    Pure Python: a latitude-sorted list plus a band prefilter answers a lookup in
    about 0.1 ms over 235k rows, so numpy and scipy are unnecessary.
    """

    def __init__(self, data_dir: Path | None = None):
        self._dir = Path(data_dir) if data_dir is not None else _DATA_DIR
        self._rows: list[tuple[float, float, str, int, str, str, str]] | None = None
        self._lats: list[float] = []
        self._admin1: dict[str, str] = {}
        self._admin2: dict[str, str] = {}
        self._countries: dict[str, str] = {}

    def _ensure_loaded(self) -> None:
        """Load on first use only, so unrelated renders pay nothing."""
        if self._rows is not None:
            return
        self._rows = []
        cities = self._dir / "cities.tsv.gz"
        if not cities.exists():
            logger.warning("Offline place dataset missing at %s; offline lookups disabled", cities)
            return
        rows = []
        with gzip.open(cities, "rt", encoding="utf-8", newline="") as f:
            for c in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE, escapechar="\\"):
                if len(c) < 7:
                    continue
                try:
                    lat, lon, pop = float(c[1]), float(c[2]), int(c[3] or 0)
                except ValueError:
                    continue
                rows.append((lat, lon, c[0], pop, c[4], c[5], c[6]))
        rows.sort(key=lambda r: r[0])
        self._rows = rows
        self._lats = [r[0] for r in rows]
        self._admin1 = _read_kv(self._dir / "admin1.tsv", gz=False)
        self._admin2 = _read_kv(self._dir / "admin2.tsv.gz", gz=True)
        self._countries = _read_kv(self._dir / "countries.tsv", gz=False)
        logger.debug("Loaded %d settlements from %s", len(rows), cities)

    def resolve(self, lat: float, lon: float, lang: str) -> PlaceName | None:
        """Nearest settlement, or None if nothing is plausibly close.

        `lang` is accepted for interface symmetry but ignored: GeoNames ships
        names, not translations.
        """
        self._ensure_loaded()
        if not self._rows:
            return None

        lo = bisect.bisect_left(self._lats, lat - _BAND_DEGREES)
        hi = bisect.bisect_right(self._lats, lat + _BAND_DEGREES)
        coslat = math.cos(math.radians(lat))

        best_sq = float("inf")
        best = None
        for i in range(lo, hi):
            row = self._rows[i]
            dy = row[0] - lat
            dx = (row[1] - lon) * coslat
            d_sq = dy * dy + dx * dx
            if d_sq < best_sq:
                best_sq, best = d_sq, row

        if best is None or math.sqrt(best_sq) * 111.0 > _MAX_MATCH_KM:
            return None

        _, _, name, pop, cc, a1, a2 = best
        if pop < _VILLAGE_MAX_POP:
            slot = "village"
        elif pop < _TOWN_MAX_POP:
            slot = "town"
        else:
            slot = "city"

        return PlaceName(
            **{slot: name},
            county=self._admin2.get(f"{cc}.{a1}.{a2}") or None,
            state=self._admin1.get(f"{cc}.{a1}") or None,
            country=self._countries.get(cc) or None,
        )


NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"

# zoom=14 returns settlement-level detail and no "road" key, so street names are
# excluded structurally rather than by filtering the response.
NOMINATIM_ZOOM = 14

# Nominatim address keys mapped onto PlaceName fields. "hamlet" is deliberately
# absent, as are road, house_number and suburb.
_ADDRESS_KEYS = ("village", "town", "city", "municipality", "county", "state", "country")


class PlaceLookupError(Exception):
    """A backend could not answer. Callers fall back rather than fail the render."""


class RateLimiter:
    """Cross-process request pacing.

    An in-process limiter is not enough: the web app and the render subprocess
    are separate processes, and two 1 req/s limiters together produce 2 req/s.
    SQLite's BEGIN IMMEDIATE gives an atomic check-and-set across processes and
    works on every platform, unlike fcntl locks.
    """

    def __init__(self, db_path: Path, min_interval_s: float):
        self.db_path = Path(db_path)
        self.min_interval_s = min_interval_s

    def wait(self) -> float:
        """Reserve the next slot, sleeping if necessary. Returns seconds slept."""
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(str(self.db_path), timeout=10.0, isolation_level=None)
            try:
                con.execute("CREATE TABLE IF NOT EXISTS ratelimit (id INTEGER PRIMARY KEY, next_at REAL)")
                con.execute("BEGIN IMMEDIATE")
                row = con.execute("SELECT next_at FROM ratelimit WHERE id = 1").fetchone()
                now = time.time()
                slot = max(now, row[0] if row else 0.0)
                con.execute(
                    "INSERT OR REPLACE INTO ratelimit (id, next_at) VALUES (1, ?)",
                    (slot + self.min_interval_s,),
                )
                con.execute("COMMIT")
            finally:
                con.close()
        except sqlite3.Error as e:
            # Never let bookkeeping break a lookup; pace conservatively instead.
            logger.debug("Rate limiter unavailable (%s); using local delay", e)
            time.sleep(self.min_interval_s)
            return self.min_interval_s

        delay = max(0.0, slot - now)
        if delay:
            time.sleep(delay)
        return delay


class NominatimBackend:
    """Online reverse geocoding via OpenStreetMap's Nominatim.

    Data (c) OpenStreetMap contributors, ODbL 1.0.
    """

    def __init__(self, session=None, limiter: RateLimiter | None = None):
        self._session = session if session is not None else requests.Session()
        self._limiter = limiter

    def resolve(self, lat: float, lon: float, lang: str) -> PlaceName | None:
        if self._limiter is not None:
            self._limiter.wait()
        try:
            response = self._session.get(
                NOMINATIM_URL,
                params={
                    "lat": lat,
                    "lon": lon,
                    "format": "jsonv2",
                    "zoom": NOMINATIM_ZOOM,
                    "accept-language": lang,
                },
                headers={"User-Agent": settings.place_user_agent},
                timeout=settings.place_request_timeout_s,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as e:  # noqa: BLE001 - any failure means "fall back"
            raise PlaceLookupError(f"Nominatim lookup failed: {e}") from e

        address = payload.get("address") or {}
        return PlaceName(**{k: address.get(k) for k in _ADDRESS_KEYS})
