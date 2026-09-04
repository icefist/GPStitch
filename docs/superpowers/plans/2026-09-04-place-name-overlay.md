# Place Name Overlay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a configurable overlay widget that displays the settlement name (village/town/city) at the current GPS position.

**Architecture:** A `PlaceResolver` with two interchangeable backends (online Nominatim, offline bundled GeoNames) sits behind an on-disk cache. Before the frame loop runs, `place_track` resolves the trip by progressive refinement — 11 samples across the whole track, then bisecting only intervals whose endpoints disagree, down to 500 m. A patched `create_place` widget renders the result through `CachingText`.

**Tech Stack:** Python 3.12+, `requests`, `sqlitedict`, stdlib `sqlite3` (cross-process rate limiting), PIL, pytest.

**Spec:** `docs/superpowers/specs/2026-09-04-place-name-overlay-design.md`

## Global Constraints

Every task's requirements implicitly include these. They are copied verbatim from the spec's Constraints section; each exists because the obvious implementation is wrong.

- **C1** Rate limiting must be cross-process (preview and render are separate processes; two in-process limiters give 2 req/s against a 1 req/s cap).
- **C2** Never attach place names to `Entry`. `Entry.interpolate` computes `end - start` for every key and catches only `KeyError`; a string raises `TypeError`, and `FrameMeta.get` interpolates on nearly every frame.
- **C3** Only compare samples produced by the same backend. Mixed-backend comparison invents phantom boundaries.
- **C4** Memoise via `weakref.WeakKeyDictionary` keyed on the framemeta object, never on `id()`. Preview rebuilds and discards framemeta constantly, and CPython reuses freed addresses — an `id()` key eventually serves the previous video's names.
- **C5** Load the cities dataset lazily, only when a layout contains a `place` widget.
- **C6** The widget value callable returns `str`, never `None`. `CachingText` raises `ValueError` on `None`. Terminal case is `""`.
- **C7** Offline gives village → county → state → country. `municipality` is online-only.
- **C8** Cache writes must never fail a render. `SqliteDict(autocommit=True)` can raise "database is locked".
- **C9** `codo` is a pint `Quantity` and may be `None`. Use `.magnitude`; fall back to time-based subdivision.
- **C10** Enforce a hard call budget — an all-different track degenerates to uniform refinement.
- **C11** Guard empty framemeta; `FrameMeta.min`/`max` fail on zero entries.
- **C12** `lang` is honoured online only; GeoNames ships names, not translations.
- **C13** Preview and render get separate call budgets.

Style: ruff, line-length 120, double quotes, `target-version = py312`. Run `uv run ruff check src tests && uv run ruff format src tests` before every commit.

---

### Task 1: Foundations — `PlaceName`, widening chain, settings

**Files:**
- Create: `src/gpstitch/services/place_resolver.py`
- Modify: `src/gpstitch/config.py:45-48`
- Modify: `pyproject.toml:29`
- Test: `tests/unit/services/test_place_resolver.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `PlaceName` (frozen dataclass, fields `village|town|city|municipality|county|state|country: str | None`, method `display_name() -> str`); `Backend` (str Enum, members `NOMINATIM = "nominatim"`, `CITIES = "cities"`); settings `place_user_agent: str`, `place_target_metres: int`, `place_initial_samples: int`, `place_max_lookups: int`, `place_preview_max_lookups: int`, `place_request_timeout_s: float`, `place_min_interval_s: float`, `place_enable_network: bool`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/services/test_place_resolver.py`:

```python
"""Tests for place name resolution."""

from gpstitch.services.place_resolver import Backend, PlaceName


class TestPlaceNameWidening:
    """display_name() walks smallest-settlement-first, then widens (spec D2, D4)."""

    def test_prefers_village_over_larger_units(self):
        p = PlaceName(village="Giethoorn", municipality="Steenwijkerland",
                      state="Overijssel", country="Netherlands")
        assert p.display_name() == "Giethoorn"

    def test_prefers_city_when_no_village_or_town(self):
        p = PlaceName(city="Bath", county="Somerset", country="United Kingdom")
        assert p.display_name() == "Bath"

    def test_widens_to_county_when_no_settlement(self):
        """Khibiny mountains: real Nominatim response has no settlement at all."""
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/services/test_place_resolver.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gpstitch.services.place_resolver'`

- [ ] **Step 3: Write minimal implementation**

Create `src/gpstitch/services/place_resolver.py`:

```python
"""Reverse geocoding of GPS positions to settlement names.

Two interchangeable backends sit behind one interface: Nominatim (online,
accurate administrative containment) and a bundled GeoNames dataset (offline,
nearest settlement). Results are cached on disk so repeat renders cost nothing.

Place data (c) OpenStreetMap contributors (ODbL 1.0) and GeoNames (CC-BY 4.0).
"""

from dataclasses import dataclass
from enum import Enum


class Backend(str, Enum):
    """Which source answered a lookup. Tracked because C3 forbids comparing across backends."""

    NOMINATIM = "nominatim"
    CITIES = "cities"


# Smallest settlement first, then widening outward (spec D2).
# "hamlet" is deliberately absent (spec D4): a person says "Giethoorn", not "Klooster".
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
        """Smallest available name, widening outward. Never None (C6)."""
        for field_name in _WIDENING_ORDER:
            value = getattr(self, field_name)
            if value:
                return value
        return ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/services/test_place_resolver.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Add settings**

In `src/gpstitch/config.py`, insert after the `gopro_config_dir` block (line ~45), before `# Allowed file extensions`:

```python
    # Place name overlay (reverse geocoding)
    # Nominatim's usage policy requires an identifying User-Agent and max 1 req/s.
    place_user_agent: str = "GPStitch (+https://github.com/Romancha/GPStitch)"
    place_min_interval_s: float = 1.0
    place_request_timeout_s: float = 5.0
    place_enable_network: bool = True
    # Progressive refinement (spec D7/D8): start coarse, bisect only where names differ.
    place_initial_samples: int = 11
    place_target_metres: int = 500
    place_max_lookups: int = 400  # C10: cap an all-different track
    place_preview_max_lookups: int = 25  # C13: preview blocks, so keep its budget small
```

- [ ] **Step 6: Declare `requests`**

In `pyproject.toml`, add to `dependencies` after `"setuptools<82",`:

```toml
    "requests>=2.32.0",
```

`requests` is currently only transitive via `gopro-overlay`. Relying on that is fragile.

- [ ] **Step 7: Verify settings load and full suite is green**

Run: `uv run python -c "from gpstitch.config import settings; print(settings.place_target_metres, settings.place_user_agent)"`
Expected: `500 GPStitch (+https://github.com/Romancha/GPStitch)`

Run: `uv run ruff check src tests && uv run ruff format src tests && uv run pytest -m "not e2e" -q`
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add src/gpstitch/services/place_resolver.py src/gpstitch/config.py pyproject.toml tests/unit/services/test_place_resolver.py
git commit -m "Add PlaceName model and place-overlay settings"
```

---

### Task 2: Dataset build script and bundled offline data

**Files:**
- Create: `scripts/build_places_dataset.py`
- Create: `src/gpstitch/data/cities.tsv.gz` (generated, ~2.3 MB)
- Create: `src/gpstitch/data/admin1.tsv` (generated, ~0.1 MB)
- Create: `src/gpstitch/data/admin2.tsv.gz` (generated, ~1 MB)
- Create: `src/gpstitch/data/countries.tsv` (generated, ~5 KB)
- Create: `src/gpstitch/data/SOURCES.md`

**Interfaces:**
- Consumes: nothing.
- Produces: four data files with these exact column orders, all tab-separated, no header row:
  - `cities.tsv.gz`: `name`, `lat`, `lon`, `population`, `country_code`, `admin1_code`, `admin2_code`
  - `admin1.tsv`: `key` (`CC.A1`), `name`
  - `admin2.tsv.gz`: `key` (`CC.A1.A2`), `name`
  - `countries.tsv`: `country_code`, `name`

This task needs network **once**, to fetch the GeoNames dump. The outputs are committed; nothing at runtime or test time downloads anything.

- [ ] **Step 1: Write the build script**

Create `scripts/build_places_dataset.py`:

```python
#!/usr/bin/env python3
"""Regenerate the bundled offline place dataset from the GeoNames dump.

Run manually when refreshing the data; the outputs are committed to the repo.

    uv run python scripts/build_places_dataset.py

Source: https://download.geonames.org/export/dump/ - licensed CC-BY 4.0.
cities500 is the smallest population floor that contains villages, which the
feature requires (spec D9).
"""

import csv
import gzip
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

BASE = "https://download.geonames.org/export/dump"
OUT = Path(__file__).resolve().parents[1] / "src" / "gpstitch" / "data"


def _fetch(name: str) -> bytes:
    url = f"{BASE}/{name}"
    print(f"fetching {url}", file=sys.stderr)
    with urllib.request.urlopen(url) as r:  # noqa: S310 - fixed, trusted host
        return r.read()


def build_cities() -> None:
    """Trim cities500 to the six columns we use; ~12.9MB download -> ~2.3MB gz."""
    raw = _fetch("cities500.zip")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        text = z.read("cities500.txt").decode("utf-8")

    rows = 0
    with gzip.open(OUT / "cities.tsv.gz", "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_NONE, escapechar="\\")
        for line in text.splitlines():
            c = line.split("\t")
            if len(c) < 15:
                continue
            # GeoNames column order: 1 name, 4 lat, 5 lon, 8 country, 10 admin1,
            # 11 admin2, 14 population
            w.writerow([c[1], c[4], c[5], c[14], c[8], c[10], c[11]])
            rows += 1
    print(f"cities.tsv.gz: {rows:,} rows", file=sys.stderr)


def build_admin(source: str, out_name: str, gz: bool) -> None:
    """admin1CodesASCII.txt / admin2Codes.txt are 'key<TAB>name<TAB>...' files."""
    text = _fetch(source).decode("utf-8")
    opener = (lambda p: gzip.open(p, "wt", encoding="utf-8", newline="")) if gz else (
        lambda p: open(p, "w", encoding="utf-8", newline="")
    )
    rows = 0
    with opener(OUT / out_name) as f:
        for line in text.splitlines():
            c = line.split("\t")
            if len(c) < 2:
                continue
            f.write(f"{c[0]}\t{c[1]}\n")
            rows += 1
    print(f"{out_name}: {rows:,} rows", file=sys.stderr)


def build_countries() -> None:
    text = _fetch("countryInfo.txt").decode("utf-8")
    rows = 0
    with open(OUT / "countries.tsv", "w", encoding="utf-8", newline="") as f:
        for line in text.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            c = line.split("\t")
            if len(c) < 5:
                continue
            f.write(f"{c[0]}\t{c[4]}\n")  # ISO code, country name
            rows += 1
    print(f"countries.tsv: {rows:,} rows", file=sys.stderr)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    build_cities()
    build_admin("admin1CodesASCII.txt", "admin1.tsv", gz=False)
    build_admin("admin2Codes.txt", "admin2.tsv.gz", gz=True)
    build_countries()
    print("done", file=sys.stderr)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the script**

Run: `uv run python scripts/build_places_dataset.py`
Expected: four files under `src/gpstitch/data/`, `cities.tsv.gz` reporting roughly 200,000 rows.

- [ ] **Step 3: Verify the outputs**

Run:

```bash
ls -la src/gpstitch/data/
uv run python -c "
import gzip
with gzip.open('src/gpstitch/data/cities.tsv.gz','rt',encoding='utf-8') as f:
    for i, ln in enumerate(f):
        if i >= 2: break
        print(ln.rstrip().split('\t'))
"
```

Expected: seven fields per row, e.g. `['Giethoorn', '52.73916', '6.07694', '2745', 'NL', '15', '...']`.

- [ ] **Step 4: Confirm Giethoorn is present**

This is the case `cities15000` failed, and the reason for `cities500` (spec R3-1).

Run:

```bash
uv run python -c "
import gzip
with gzip.open('src/gpstitch/data/cities.tsv.gz','rt',encoding='utf-8') as f:
    hits=[l for l in f if l.startswith('Giethoorn\t')]
print('Giethoorn rows:', len(hits)); print(hits[:1])
"
```

Expected: at least 1. If 0, the dataset is wrong — stop and re-check the download.

- [ ] **Step 5: Record provenance**

Create `src/gpstitch/data/SOURCES.md`:

```markdown
# Bundled place data

Generated by `scripts/build_places_dataset.py`. Do not edit by hand.

| File | Source | Notes |
|---|---|---|
| `cities.tsv.gz` | `cities500.zip` | Settlements with population >= 500 |
| `admin1.tsv` | `admin1CodesASCII.txt` | Admin-1 (state/province) code -> name |
| `admin2.tsv.gz` | `admin2Codes.txt` | Admin-2 (county) code -> name |
| `countries.tsv` | `countryInfo.txt` | ISO 3166-1 alpha-2 -> country name |

All from <https://download.geonames.org/export/dump/>, licensed
[CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/).

Regenerate with:

    uv run python scripts/build_places_dataset.py
```

- [ ] **Step 6: Confirm the data ships in the wheel**

Run: `uv build --wheel -o /tmp/wheelcheck && uv run python -c "
import zipfile, glob
w = sorted(glob.glob('/tmp/wheelcheck/*.whl'))[-1]
print([n for n in zipfile.ZipFile(w).namelist() if '/data/' in n])
"`
Expected: all four data files listed. (`[tool.hatch.build.targets.wheel] packages = ["src/gpstitch"]` already ships non-`.py` files; this confirms it.)

- [ ] **Step 7: Commit**

```bash
git add scripts/build_places_dataset.py src/gpstitch/data/
git commit -m "Add GeoNames offline place dataset and its build script"
```

---

### Task 3: `CitiesBackend` — offline nearest-settlement lookup

**Files:**
- Modify: `src/gpstitch/services/place_resolver.py`
- Test: `tests/unit/services/test_cities_backend.py`

**Interfaces:**
- Consumes: `PlaceName` from Task 1; the data files from Task 2.
- Produces: `CitiesBackend(data_dir: Path | None = None)` with `resolve(lat: float, lon: float, lang: str) -> PlaceName | None`. Loading is lazy (C5).

Tests use a tiny synthetic fixture, never the real 200k-row file (spec R4-4).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/services/test_cities_backend.py`:

```python
"""Tests for the offline GeoNames backend."""

import gzip

import pytest

from gpstitch.services.place_resolver import CitiesBackend


@pytest.fixture
def tiny_data_dir(tmp_path):
    """A 3-settlement dataset. Never load the real 200k-row file in tests."""
    with gzip.open(tmp_path / "cities.tsv.gz", "wt", encoding="utf-8") as f:
        # name, lat, lon, population, cc, admin1, admin2
        f.write("Giethoorn\t52.73916\t6.07694\t2745\tNL\t15\t1700\n")
        f.write("Steenwijk\t52.78889\t6.11944\t17000\tNL\t15\t1700\n")
        f.write("Bath\t51.37500\t-2.36667\t94782\tGB\tENG\tSOM\n")
    (tmp_path / "admin1.tsv").write_text("NL.15\tOverijssel\nGB.ENG\tEngland\n", encoding="utf-8")
    with gzip.open(tmp_path / "admin2.tsv.gz", "wt", encoding="utf-8") as f:
        f.write("NL.15.1700\tSteenwijkerland\nGB.ENG.SOM\tSomerset\n")
    (tmp_path / "countries.tsv").write_text("NL\tNetherlands\nGB\tUnited Kingdom\n", encoding="utf-8")
    return tmp_path


class TestCitiesBackend:
    def test_finds_nearest_settlement(self, tiny_data_dir):
        got = CitiesBackend(tiny_data_dir).resolve(52.7402, 6.0781, "en")
        assert got.display_name() == "Giethoorn"

    def test_small_population_lands_in_village_slot(self, tiny_data_dir):
        got = CitiesBackend(tiny_data_dir).resolve(52.7402, 6.0781, "en")
        assert got.village == "Giethoorn"
        assert got.city is None

    def test_large_population_lands_in_city_slot(self, tiny_data_dir):
        got = CitiesBackend(tiny_data_dir).resolve(51.3750, -2.36667, "en")
        assert got.city == "Bath"
        assert got.village is None

    def test_resolves_admin_codes_to_names(self, tiny_data_dir):
        """C7: offline reaches county, state and country - not municipality."""
        got = CitiesBackend(tiny_data_dir).resolve(52.7402, 6.0781, "en")
        assert got.county == "Steenwijkerland"
        assert got.state == "Overijssel"
        assert got.country == "Netherlands"
        assert got.municipality is None

    def test_picks_the_closer_of_two_nearby_settlements(self, tiny_data_dir):
        """Near Steenwijk, not Giethoorn."""
        got = CitiesBackend(tiny_data_dir).resolve(52.7880, 6.1190, "en")
        assert got.display_name() == "Steenwijk"

    def test_returns_none_when_nothing_within_the_band(self, tiny_data_dir):
        """Mid-Pacific: no settlement anywhere near."""
        assert CitiesBackend(tiny_data_dir).resolve(-40.0, -140.0, "en") is None

    def test_missing_data_dir_returns_none_and_does_not_raise(self, tmp_path):
        assert CitiesBackend(tmp_path / "nope").resolve(52.0, 6.0, "en") is None

    def test_loading_is_lazy(self, tiny_data_dir):
        """C5: constructing must not read the dataset."""
        backend = CitiesBackend(tiny_data_dir)
        assert backend._rows is None
        backend.resolve(52.7402, 6.0781, "en")
        assert backend._rows is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/services/test_cities_backend.py -v`
Expected: FAIL — `ImportError: cannot import name 'CitiesBackend'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/gpstitch/services/place_resolver.py`:

```python
import bisect
import csv
import gzip
import logging
import math
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Population thresholds sorting a settlement into a hierarchy slot. GeoNames has
# no village/town/city distinction, so this is a heuristic - the exact cut points
# matter little because the user accepted coarse accuracy.
_VILLAGE_MAX_POP = 10_000
_TOWN_MAX_POP = 100_000

# Latitude half-window scanned around a query. 0.5 deg is ~55km, comfortably
# wider than any gap between settlements at cities500 density, and keeps the
# scan to ~1,500 of 200,000 rows.
_BAND_DEGREES = 0.5

# Beyond this, treat the result as "nowhere near a settlement".
_MAX_MATCH_KM = 75.0


def _read_kv(path: Path, gz: bool) -> dict[str, str]:
    """Read a two-column key/name TSV into a dict. Missing file yields {}."""
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
    ~0.1 ms over 200k rows, so numpy/scipy are unnecessary.
    """

    def __init__(self, data_dir: Path | None = None):
        self._dir = Path(data_dir) if data_dir is not None else _DATA_DIR
        self._rows: list[tuple[float, float, str, int, str, str, str]] | None = None
        self._lats: list[float] = []
        self._admin1: dict[str, str] = {}
        self._admin2: dict[str, str] = {}
        self._countries: dict[str, str] = {}

    def _ensure_loaded(self) -> None:
        """Load on first use only (C5)."""
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
        names, not translations (C12).
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
```

Move the new imports to the top of the file with the existing ones and let ruff sort them.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/services/test_cities_backend.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Sanity-check against the real dataset**

Run:

```bash
uv run python -c "
from gpstitch.services.place_resolver import CitiesBackend
b = CitiesBackend()
print('Giethoorn area:', b.resolve(52.7402, 6.0781, 'en'))
print('mid-Pacific  :', b.resolve(-40.0, -140.0, 'en'))
"
```

Expected: the first names Giethoorn (or an immediate neighbour); the second prints `None`.

- [ ] **Step 6: Commit**

```bash
git add src/gpstitch/services/place_resolver.py tests/unit/services/test_cities_backend.py
git commit -m "Add offline GeoNames place backend"
```

---

### Task 4: `NominatimBackend` and cross-process rate limiting

**Files:**
- Modify: `src/gpstitch/services/place_resolver.py`
- Test: `tests/unit/services/test_nominatim_backend.py`

**Interfaces:**
- Consumes: `PlaceName` (Task 1), settings (Task 1).
- Produces: `RateLimiter(db_path: Path, min_interval_s: float)` with `wait() -> float` (returns seconds slept); `NominatimBackend(session=None, limiter=None)` with `resolve(lat, lon, lang) -> PlaceName | None`, raising `PlaceLookupError` on any network or HTTP failure; `PlaceLookupError(Exception)`.

No test touches the network — `session` is injected.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/services/test_nominatim_backend.py`:

```python
"""Tests for the online Nominatim backend and its cross-process rate limiter."""

import time
from unittest.mock import MagicMock

import pytest

from gpstitch.services.place_resolver import (
    NominatimBackend,
    PlaceLookupError,
    RateLimiter,
)

# Trimmed from a real Nominatim jsonv2 response for Giethoorn.
GIETHOORN = {
    "addresstype": "hamlet",
    "address": {
        "hamlet": "Klooster",
        "village": "Giethoorn",
        "municipality": "Steenwijkerland",
        "state": "Overijssel",
        "country": "Netherlands",
        "postcode": "8355 AB",
        "country_code": "nl",
    },
}

# Real response for the Khibiny mountains: no settlement of any kind.
WILDERNESS = {
    "addresstype": "county",
    "address": {
        "county": "Kirovsk Urban Okrug",
        "state": "Murmansk Oblast",
        "country": "Russia",
        "country_code": "ru",
    },
}


def _session(payload, status=200):
    s = MagicMock()
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    s.get.return_value = r
    return s


class TestNominatimParsing:
    def test_maps_address_keys_onto_placename(self):
        got = NominatimBackend(session=_session(GIETHOORN), limiter=None).resolve(52.74, 6.07, "en")
        assert got.village == "Giethoorn"
        assert got.municipality == "Steenwijkerland"
        assert got.state == "Overijssel"
        assert got.country == "Netherlands"

    def test_ignores_hamlet_key(self):
        """D4: hamlet is not part of the hierarchy - 'Giethoorn', not 'Klooster'."""
        got = NominatimBackend(session=_session(GIETHOORN), limiter=None).resolve(52.74, 6.07, "en")
        assert got.display_name() == "Giethoorn"

    def test_wilderness_yields_no_settlement_but_still_widens(self):
        got = NominatimBackend(session=_session(WILDERNESS), limiter=None).resolve(67.69, 33.59, "en")
        assert got.village is None and got.town is None and got.city is None
        assert got.display_name() == "Kirovsk Urban Okrug"

    def test_empty_address_yields_empty_display_name(self):
        """Open ocean: Nominatim returns no address at all (C6)."""
        got = NominatimBackend(session=_session({"address": {}}), limiter=None).resolve(0.0, -140.0, "en")
        assert got.display_name() == ""


class TestNominatimRequest:
    def test_sends_identifying_user_agent_and_language(self):
        s = _session(GIETHOORN)
        NominatimBackend(session=s, limiter=None).resolve(52.74, 6.07, "de")
        _, kwargs = s.get.call_args
        assert "GPStitch" in kwargs["headers"]["User-Agent"]
        assert kwargs["params"]["accept-language"] == "de"

    def test_requests_zoom_14_so_no_road_key_is_returned(self):
        """Street names are excluded structurally by zoom, not by filtering."""
        s = _session(GIETHOORN)
        NominatimBackend(session=s, limiter=None).resolve(52.74, 6.07, "en")
        assert s.get.call_args[1]["params"]["zoom"] == 14

    def test_network_error_raises_place_lookup_error(self):
        s = MagicMock()
        s.get.side_effect = OSError("connection refused")
        with pytest.raises(PlaceLookupError):
            NominatimBackend(session=s, limiter=None).resolve(52.74, 6.07, "en")

    def test_http_error_raises_place_lookup_error(self):
        s = MagicMock()
        r = MagicMock()
        r.raise_for_status.side_effect = OSError("429 Too Many Requests")
        s.get.return_value = r
        with pytest.raises(PlaceLookupError):
            NominatimBackend(session=s, limiter=None).resolve(52.74, 6.07, "en")


class TestRateLimiter:
    def test_first_call_does_not_wait(self, tmp_path):
        assert RateLimiter(tmp_path / "rl.sqlite", 1.0).wait() == pytest.approx(0.0, abs=0.05)

    def test_second_call_waits_the_interval(self, tmp_path):
        limiter = RateLimiter(tmp_path / "rl.sqlite", 0.3)
        limiter.wait()
        t0 = time.monotonic()
        limiter.wait()
        assert time.monotonic() - t0 >= 0.25

    def test_separate_instances_share_state_via_the_file(self, tmp_path):
        """C1: preview and render are different processes sharing one cap."""
        db = tmp_path / "rl.sqlite"
        RateLimiter(db, 0.3).wait()
        t0 = time.monotonic()
        RateLimiter(db, 0.3).wait()
        assert time.monotonic() - t0 >= 0.25
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/services/test_nominatim_backend.py -v`
Expected: FAIL — `ImportError: cannot import name 'NominatimBackend'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/gpstitch/services/place_resolver.py`:

```python
import sqlite3
import time

import requests

from gpstitch.config import settings

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"

# zoom=14 returns settlement-level detail and no "road" key, so street names are
# excluded structurally rather than by filtering the response.
NOMINATIM_ZOOM = 14

# Nominatim address keys mapped onto PlaceName fields. "hamlet" is deliberately
# absent (D4), as are road/house_number/suburb.
_ADDRESS_KEYS = ("village", "town", "city", "municipality", "county", "state", "country")


class PlaceLookupError(Exception):
    """A backend could not answer. Callers fall back rather than fail the render."""


class RateLimiter:
    """Cross-process request pacing (C1).

    An in-process limiter is not enough: the web app and the render subprocess
    are separate processes, and two 1 req/s limiters produce 2 req/s. SQLite's
    BEGIN IMMEDIATE gives an atomic check-and-set across processes and works on
    every platform, unlike fcntl locks.
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
                con.execute("INSERT OR REPLACE INTO ratelimit (id, next_at) VALUES (1, ?)",
                            (slot + self.min_interval_s,))
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/services/test_nominatim_backend.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add src/gpstitch/services/place_resolver.py tests/unit/services/test_nominatim_backend.py
git commit -m "Add Nominatim backend with cross-process rate limiting"
```

---

### Task 5: `PlaceResolver` — cache and backend fallback

**Files:**
- Modify: `src/gpstitch/services/place_resolver.py`
- Test: `tests/unit/services/test_place_resolver_cache.py`

**Interfaces:**
- Consumes: `PlaceName`, `Backend`, `CitiesBackend`, `NominatimBackend`, `PlaceLookupError`, `RateLimiter`.
- Produces: `PlaceResolver(online=None, offline=None, cache_path=None, enable_network=True)` with `resolve(lat: float, lon: float, lang: str) -> tuple[PlaceName | None, Backend]`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/services/test_place_resolver_cache.py`:

```python
"""Tests for resolver caching and backend fallback."""

from unittest.mock import MagicMock

from gpstitch.services.place_resolver import (
    Backend,
    PlaceLookupError,
    PlaceName,
    PlaceResolver,
)

BATH = PlaceName(city="Bath", county="Somerset", country="United Kingdom")
BATH_OFFLINE = PlaceName(city="Bath", county="Somerset", country="United Kingdom")


def _backend(result=None, error=None):
    b = MagicMock()
    b.resolve.side_effect = error if error else None
    if not error:
        b.resolve.return_value = result
    return b


class TestBackendSelection:
    def test_uses_online_when_it_answers(self, tmp_path):
        online, offline = _backend(BATH), _backend(BATH_OFFLINE)
        place, backend = PlaceResolver(online, offline, tmp_path / "c.sqlite").resolve(51.37, -2.36, "en")
        assert place == BATH
        assert backend is Backend.NOMINATIM
        offline.resolve.assert_not_called()

    def test_falls_back_to_offline_on_lookup_error(self, tmp_path):
        online = _backend(error=PlaceLookupError("boom"))
        offline = _backend(BATH_OFFLINE)
        place, backend = PlaceResolver(online, offline, tmp_path / "c.sqlite").resolve(51.37, -2.36, "en")
        assert place == BATH_OFFLINE
        assert backend is Backend.CITIES

    def test_skips_network_entirely_when_disabled(self, tmp_path):
        online, offline = _backend(BATH), _backend(BATH_OFFLINE)
        r = PlaceResolver(online, offline, tmp_path / "c.sqlite", enable_network=False)
        _, backend = r.resolve(51.37, -2.36, "en")
        assert backend is Backend.CITIES
        online.resolve.assert_not_called()

    def test_both_failing_returns_none(self, tmp_path):
        online = _backend(error=PlaceLookupError("boom"))
        offline = _backend(None)
        place, _ = PlaceResolver(online, offline, tmp_path / "c.sqlite").resolve(0.0, -140.0, "en")
        assert place is None


class TestCaching:
    def test_second_identical_lookup_hits_cache(self, tmp_path):
        online = _backend(BATH)
        r = PlaceResolver(online, _backend(None), tmp_path / "c.sqlite")
        r.resolve(51.37, -2.36, "en")
        r.resolve(51.37, -2.36, "en")
        assert online.resolve.call_count == 1

    def test_nearby_points_share_a_cache_bucket(self, tmp_path):
        """Keys round to 3dp (~110m), so a stationary rider reuses one entry."""
        online = _backend(BATH)
        r = PlaceResolver(online, _backend(None), tmp_path / "c.sqlite")
        r.resolve(51.370001, -2.360001, "en")
        r.resolve(51.370002, -2.360002, "en")
        assert online.resolve.call_count == 1

    def test_different_language_is_a_different_key(self, tmp_path):
        online = _backend(BATH)
        r = PlaceResolver(online, _backend(None), tmp_path / "c.sqlite")
        r.resolve(51.37, -2.36, "en")
        r.resolve(51.37, -2.36, "de")
        assert online.resolve.call_count == 2

    def test_cache_survives_a_new_resolver_instance(self, tmp_path):
        cache = tmp_path / "c.sqlite"
        PlaceResolver(_backend(BATH), _backend(None), cache).resolve(51.37, -2.36, "en")
        online2 = _backend(BATH)
        PlaceResolver(online2, _backend(None), cache).resolve(51.37, -2.36, "en")
        online2.resolve.assert_not_called()

    def test_unwritable_cache_does_not_break_resolution(self, tmp_path):
        """C8: a locked or unwritable cache must never fail a render."""
        bad = tmp_path / "nonexistent-dir" / "sub" / "c.sqlite"
        place, backend = PlaceResolver(_backend(BATH), _backend(None), bad).resolve(51.37, -2.36, "en")
        assert place == BATH
        assert backend is Backend.NOMINATIM
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/services/test_place_resolver_cache.py -v`
Expected: FAIL — `ImportError: cannot import name 'PlaceResolver'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/gpstitch/services/place_resolver.py`:

```python
from sqlitedict import SqliteDict

# ~110m buckets: a stationary rider or slow hiker reuses one cache entry.
_CACHE_PRECISION = 3


class PlaceResolver:
    """Cached reverse geocoding with online-first, offline-fallback behaviour."""

    def __init__(
        self,
        online: "NominatimBackend | None" = None,
        offline: "CitiesBackend | None" = None,
        cache_path: Path | None = None,
        enable_network: bool | None = None,
    ):
        cache_dir = settings.gopro_config_dir
        self._online = online if online is not None else NominatimBackend(
            limiter=RateLimiter(cache_dir / "placeratelimit.sqlite", settings.place_min_interval_s)
        )
        self._offline = offline if offline is not None else CitiesBackend()
        self._cache_path = Path(cache_path) if cache_path is not None else cache_dir / "placecache.sqlite"
        self._enable_network = settings.place_enable_network if enable_network is None else enable_network

    def _key(self, lat: float, lon: float, lang: str) -> str:
        return f"{round(lat, _CACHE_PRECISION)},{round(lon, _CACHE_PRECISION)},{lang}"

    def _cache_get(self, key: str):
        try:
            with SqliteDict(filename=str(self._cache_path), autocommit=True) as db:
                return db.get(key)
        except Exception as e:  # noqa: BLE001 - C8: cache problems are never fatal
            logger.debug("Place cache read failed (%s)", e)
            return None

    def _cache_put(self, key: str, value) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            with SqliteDict(filename=str(self._cache_path), autocommit=True) as db:
                db[key] = value
        except Exception as e:  # noqa: BLE001 - C8
            logger.debug("Place cache write failed (%s)", e)

    def resolve(self, lat: float, lon: float, lang: str) -> tuple[PlaceName | None, Backend]:
        """Resolve a position, reporting which backend answered.

        The backend is part of the return value because C3 forbids comparing
        samples that came from different sources.
        """
        key = self._key(lat, lon, lang)
        cached = self._cache_get(key)
        if cached is not None:
            place, backend = cached
            return place, Backend(backend)

        if self._enable_network:
            try:
                place = self._online.resolve(lat, lon, lang)
                self._cache_put(key, (place, Backend.NOMINATIM.value))
                return place, Backend.NOMINATIM
            except PlaceLookupError as e:
                logger.info("Falling back to offline place data: %s", e)

        place = self._offline.resolve(lat, lon, lang)
        self._cache_put(key, (place, Backend.CITIES.value))
        return place, Backend.CITIES
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/services/test_place_resolver_cache.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add src/gpstitch/services/place_resolver.py tests/unit/services/test_place_resolver_cache.py
git commit -m "Add place resolver with disk cache and offline fallback"
```

---

### Task 6: `PlaceTrack` — progressive, disagreement-gated sampling

**Files:**
- Create: `src/gpstitch/services/place_track.py`
- Test: `tests/unit/services/test_place_track.py`

**Interfaces:**
- Consumes: `PlaceName`, `Backend`, `PlaceResolver`.
- Produces: `PlaceSample` (frozen dataclass: `dt: datetime`, `place: PlaceName | None`, `backend: Backend`); `PlaceTrack` with `.samples: list[PlaceSample]` and `at(dt: datetime) -> str`; `build_place_track(framemeta, resolver, lang: str, target_metres: int, initial_samples: int, max_lookups: int) -> PlaceTrack`.

This is the heart of the feature. The algorithm is the user's design: coarse coverage first, then bisect only where names disagree, so an interrupted run leaves a complete coarser track rather than a half-resolved one.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/services/test_place_track.py`:

```python
"""Tests for progressive, disagreement-gated place sampling."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from gpstitch.services.place_resolver import Backend, PlaceName
from gpstitch.services.place_track import PlaceSample, PlaceTrack, build_place_track

T0 = datetime(2026, 9, 4, 12, 0, 0)


@dataclass
class FakePoint:
    lat: float
    lon: float


class FakeEntry:
    """Stands in for gopro_overlay Entry: dt, point, and a codo magnitude."""

    def __init__(self, dt, lat, lon, codo_m):
        self.dt = dt
        self.point = FakePoint(lat, lon)
        self.codo = FakeQuantity(codo_m) if codo_m is not None else None


class FakeQuantity:
    """Stands in for a pint Quantity (C9)."""

    def __init__(self, magnitude):
        self.magnitude = magnitude


class FakeFrameMeta:
    def __init__(self, entries):
        self.frames = {i: e for i, e in enumerate(entries)}


def _straight_track(n=101, total_m=50_000):
    """n entries evenly spaced along a line, with cumulative distance."""
    return FakeFrameMeta([
        FakeEntry(T0 + timedelta(seconds=i * 10), 51.0 + i * 0.001, -2.0, total_m * i / (n - 1))
        for i in range(n)
    ])


class CountingResolver:
    """Returns a name based on latitude bands, and counts calls."""

    def __init__(self, boundaries=(), backend=Backend.NOMINATIM):
        self.calls = 0
        self.boundaries = boundaries
        self.backend = backend

    def resolve(self, lat, lon, lang):
        self.calls += 1
        idx = sum(1 for b in self.boundaries if lat >= b)
        return PlaceName(city=f"Town{idx}"), self.backend


class TestInitialCoverage:
    def test_level_zero_covers_the_whole_track(self):
        r = CountingResolver()
        track = build_place_track(_straight_track(), r, "en", 500, 11, 400)
        assert r.calls == 11
        assert track.samples[0].dt == T0
        assert track.samples[-1].dt == T0 + timedelta(seconds=1000)

    def test_uniform_area_needs_no_refinement(self):
        """All samples agree, so nothing is bisected."""
        r = CountingResolver()
        build_place_track(_straight_track(), r, "en", 500, 11, 400)
        assert r.calls == 11


class TestDisagreementGating:
    def test_one_boundary_triggers_refinement(self):
        r = CountingResolver(boundaries=(51.05,))
        build_place_track(_straight_track(), r, "en", 500, 11, 400)
        assert r.calls > 11

    def test_refinement_stays_far_below_uniform_sampling(self):
        """50km at 500m would be ~100 calls; gating should cost far less."""
        r = CountingResolver(boundaries=(51.05,))
        build_place_track(_straight_track(), r, "en", 500, 11, 400)
        assert r.calls < 60

    def test_respects_the_call_budget(self):
        """C10: an all-different track must not run away."""
        r = CountingResolver(boundaries=tuple(51.0 + i * 0.002 for i in range(50)))
        build_place_track(_straight_track(), r, "en", 500, 11, 20)
        assert r.calls <= 20

    def test_does_not_compare_across_backends(self):
        """C3: a backend switch stops refinement rather than inventing a boundary."""

        class SwitchingResolver:
            def __init__(self):
                self.calls = 0

            def resolve(self, lat, lon, lang):
                self.calls += 1
                if self.calls <= 11:
                    return PlaceName(city="Same"), Backend.NOMINATIM
                return PlaceName(city="DifferentWording"), Backend.CITIES

        r = SwitchingResolver()
        build_place_track(_straight_track(), r, "en", 500, 11, 400)
        assert r.calls == 11


class TestLookup:
    def test_returns_name_at_an_exact_sample(self):
        track = PlaceTrack([PlaceSample(T0, PlaceName(city="Bath"), Backend.NOMINATIM)])
        assert track.at(T0) == "Bath"

    def test_holds_the_last_name_between_samples(self):
        track = PlaceTrack([
            PlaceSample(T0, PlaceName(city="Bath"), Backend.NOMINATIM),
            PlaceSample(T0 + timedelta(minutes=10), PlaceName(city="Bristol"), Backend.NOMINATIM),
        ])
        assert track.at(T0 + timedelta(minutes=5)) == "Bath"

    def test_before_the_first_sample_uses_the_first_name(self):
        track = PlaceTrack([PlaceSample(T0, PlaceName(city="Bath"), Backend.NOMINATIM)])
        assert track.at(T0 - timedelta(hours=1)) == "Bath"

    def test_after_the_last_sample_uses_the_last_name(self):
        track = PlaceTrack([PlaceSample(T0, PlaceName(city="Bath"), Backend.NOMINATIM)])
        assert track.at(T0 + timedelta(hours=1)) == "Bath"

    def test_empty_track_returns_empty_string(self):
        """C6."""
        assert PlaceTrack([]).at(T0) == ""

    def test_none_place_returns_empty_string(self):
        """C6: open ocean resolves to nothing at all."""
        track = PlaceTrack([PlaceSample(T0, None, Backend.CITIES)])
        assert track.at(T0) == ""

    def test_none_dt_returns_empty_string(self):
        """C6: entry() is None before the first draw."""
        assert PlaceTrack([PlaceSample(T0, PlaceName(city="Bath"), Backend.NOMINATIM)]).at(None) == ""


class TestDegenerateInputs:
    def test_empty_framemeta_yields_empty_track(self):
        """C11."""
        r = CountingResolver()
        assert build_place_track(FakeFrameMeta([]), r, "en", 500, 11, 400).samples == []
        assert r.calls == 0

    def test_single_entry_needs_one_lookup(self):
        r = CountingResolver()
        track = build_place_track(_straight_track(n=1, total_m=0), r, "en", 500, 11, 400)
        assert r.calls == 1
        assert len(track.samples) == 1

    def test_track_shorter_than_target_stops_at_level_zero(self):
        r = CountingResolver(boundaries=(51.0005,))
        build_place_track(_straight_track(n=11, total_m=200), r, "en", 500, 11, 400)
        assert r.calls == 11

    def test_missing_codo_falls_back_to_time_subdivision(self):
        """C9: codo is None when dist is missing."""
        fm = FakeFrameMeta([
            FakeEntry(T0 + timedelta(seconds=i * 10), 51.0 + i * 0.001, -2.0, None) for i in range(101)
        ])
        r = CountingResolver()
        track = build_place_track(fm, r, "en", 500, 11, 400)
        assert r.calls == 11
        assert len(track.samples) == 11

    def test_entries_without_a_point_are_skipped(self):
        """C6: no GPS fix."""
        entries = [FakeEntry(T0 + timedelta(seconds=i * 10), 51.0 + i * 0.001, -2.0, i * 500.0)
                   for i in range(11)]
        entries[3].point = None
        r = CountingResolver()
        track = build_place_track(FakeFrameMeta(entries), r, "en", 500, 11, 400)
        assert all(s.place is not None for s in track.samples)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/services/test_place_track.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gpstitch.services.place_track'`

- [ ] **Step 3: Write minimal implementation**

Create `src/gpstitch/services/place_track.py`:

```python
"""Resolve a whole trip's place names before the frame loop runs.

Place names deliberately do NOT live on Entry objects (C2): Entry.interpolate
subtracts every field and catches only KeyError, so a string field raises
TypeError - and FrameMeta.get interpolates on nearly every frame. A separate
time-indexed track sidesteps that entirely.

Sampling refines progressively (spec D7/D8): eleven samples spread across the
whole trip first, then bisection of only those intervals whose endpoints
resolved to different names. Every level is complete in itself, so abandoning
the process - budget exhausted, API down - leaves a usable coarser track rather
than a half-resolved one.
"""

import bisect
import logging
from dataclasses import dataclass
from datetime import datetime

from gpstitch.services.place_resolver import Backend, PlaceName

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlaceSample:
    """One resolved point. `backend` is retained because C3 forbids cross-backend comparison."""

    dt: datetime
    place: PlaceName | None
    backend: Backend


class PlaceTrack:
    """Time-indexed place names with last-known-value lookup."""

    def __init__(self, samples: list[PlaceSample]):
        self.samples = sorted(samples, key=lambda s: s.dt)
        self._dts = [s.dt for s in self.samples]

    def at(self, dt: datetime | None) -> str:
        """Name in effect at `dt`. Always a str, never None (C6)."""
        if not self.samples or dt is None:
            return ""
        idx = bisect.bisect_right(self._dts, dt) - 1
        if idx < 0:
            idx = 0  # before the first sample: hold the first known name
        place = self.samples[idx].place
        return place.display_name() if place is not None else ""


def _ordered_entries(framemeta) -> list:
    """Entries in time order, skipping any without a usable GPS point (C6)."""
    entries = [framemeta.frames[k] for k in sorted(framemeta.frames)]
    return [e for e in entries if getattr(e, "point", None) is not None]


# Nominal speed used to convert the metre target into a time threshold for
# tracks that carry no distance data (C9). ~50 km/h sits between hiking and
# driving; the user accepted coarse accuracy, so a rough equivalence is fine.
_NOMINAL_SPEED_MS = 13.9


def _progress_axis(entries: list, target_metres: float) -> tuple[list[float], float]:
    """Return (axis values, stop threshold expressed in the axis's own units).

    Prefers cumulative distance in metres. Falls back to elapsed seconds when
    codo is missing (C9) - and converts the target too, so the stop condition
    never compares seconds against metres.
    """
    codos = []
    for e in entries:
        codo = getattr(e, "codo", None)
        if codo is None:
            codos = []
            break
        codos.append(float(getattr(codo, "magnitude", codo)))
    if codos and codos[-1] > 0:
        return codos, float(target_metres)
    t0 = entries[0].dt
    return [(e.dt - t0).total_seconds() for e in entries], target_metres / _NOMINAL_SPEED_MS


def build_place_track(
    framemeta,
    resolver,
    lang: str,
    target_metres: int,
    initial_samples: int,
    max_lookups: int,
) -> PlaceTrack:
    """Resolve a trip's place names, coarse to fine.

    `resolver.resolve(lat, lon, lang)` must return `(PlaceName | None, Backend)`.
    """
    entries = _ordered_entries(framemeta)
    if not entries:  # C11
        return PlaceTrack([])

    axis, stop_threshold = _progress_axis(entries, target_metres)
    budget = [max_lookups]
    resolved: dict[int, PlaceSample] = {}

    def sample(idx: int) -> PlaceSample | None:
        """Resolve entry `idx` once, honouring the budget (C10)."""
        if idx in resolved:
            return resolved[idx]
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        entry = entries[idx]
        try:
            place, backend = resolver.resolve(entry.point.lat, entry.point.lon, lang)
        except Exception as e:  # noqa: BLE001 - a failed lookup never fails a render
            logger.info("Place lookup failed at index %d: %s", idx, e)
            return None
        s = PlaceSample(entry.dt, place, backend)
        resolved[idx] = s
        return s

    # --- Level 0: spread `initial_samples` evenly, covering the whole trip.
    n = len(entries)
    count = max(1, min(initial_samples, n))
    if count == 1:
        level0 = [0]
    else:
        level0 = sorted({round(i * (n - 1) / (count - 1)) for i in range(count)})
    for idx in level0:
        sample(idx)

    # --- Refinement: bisect only intervals whose endpoints disagree (D8).
    pending = [(level0[i], level0[i + 1]) for i in range(len(level0) - 1)]
    while pending and budget[0] > 0:
        lo, hi = pending.pop(0)
        s_lo, s_hi = resolved.get(lo), resolved.get(hi)
        if s_lo is None or s_hi is None:
            continue
        # C3: never compare names that came from different sources.
        if s_lo.backend is not s_hi.backend:
            continue
        if s_lo.place == s_hi.place:
            continue
        interval = axis[hi] - axis[lo]
        if interval <= stop_threshold or hi - lo <= 1:
            continue
        mid = (lo + hi) // 2
        if sample(mid) is None:
            continue
        pending.append((lo, mid))
        pending.append((mid, hi))

    return PlaceTrack(list(resolved.values()))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/services/test_place_track.py -v`
Expected: PASS (18 tests)

- [ ] **Step 5: Commit**

```bash
git add src/gpstitch/services/place_track.py tests/unit/services/test_place_track.py
git commit -m "Add progressive disagreement-gated place sampling"
```

---

### Task 7: `create_place` widget patch

**Files:**
- Create: `src/gpstitch/patches/place_patches.py`
- Modify: `src/gpstitch/patches/__init__.py:1-9` (docstring) and `:31-38` (registration)
- Test: `tests/unit/patches/test_place_patches.py`

**Interfaces:**
- Consumes: `build_place_track`, `PlaceTrack` (Task 6); `PlaceResolver` (Task 5).
- Produces: `patch_place_widget() -> None`; `track_for(framemeta, lang, target_m, resolver=None, max_lookups=None) -> PlaceTrack` (memoised per C4).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/patches/test_place_patches.py`:

```python
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


class TestTrackMemoisation:
    def test_same_arguments_reuse_one_track(self):
        fm, r = FakeFrameMeta(), StubResolver()
        a = track_for(fm, "en", 500, resolver=r)
        b = track_for(fm, "en", 500, resolver=r)
        assert a is b

    def test_different_language_builds_a_separate_track(self):
        """C4: two place widgets with different lang must not collide."""
        fm = FakeFrameMeta()
        a = track_for(fm, "en", 500, resolver=StubResolver("Bath"))
        b = track_for(fm, "de", 500, resolver=StubResolver("Bad"))
        assert a is not b
        assert b.at(T0) == "Bad"


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
        from gopro_overlay import layout_xml
        from PIL import ImageFont

        patch_place_widget()
        fm = FakeFrameMeta()
        factory = layout_xml.Widgets(
            font=lambda size: ImageFont.load_default(),
            privacy=None, renderer=None, framemeta=fm, converters=None,
        )
        element = ET.fromstring('<component type="place" x="10" y="20" size="24" lang="en"/>')
        entry_holder = {"e": fm.frames[0]}
        widget = factory.create_place(element, entry=lambda: entry_holder["e"],
                                      resolver=StubResolver("Bath"))
        assert widget.value() == "Bath"

    def test_widget_returns_empty_string_when_entry_is_none(self):
        """C6: entry() is None before the first draw; CachingText raises on None."""
        from gopro_overlay import layout_xml
        from PIL import ImageFont

        patch_place_widget()
        factory = layout_xml.Widgets(
            font=lambda size: ImageFont.load_default(),
            privacy=None, renderer=None, framemeta=FakeFrameMeta(), converters=None,
        )
        element = ET.fromstring('<component type="place"/>')
        widget = factory.create_place(element, entry=lambda: None, resolver=StubResolver())
        assert widget.value() == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/patches/test_place_patches.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gpstitch.patches.place_patches'`

- [ ] **Step 3: Write minimal implementation**

Create `src/gpstitch/patches/place_patches.py`:

```python
"""Patch adding a `place` component that renders the current settlement name.

The track cannot be built when patches are applied - gopro-dashboard.py loads
its data afterwards - so it is built lazily at widget-creation time, which
happens after the data is loaded and where `self.framemeta` is available.
"""

import logging
import weakref

logger = logging.getLogger(__name__)

# Memoised tracks, keyed on the framemeta OBJECT via weakref (C4).
#
# id(framemeta) would be wrong: preview rebuilds and discards framemeta on every
# request, and CPython reuses freed addresses, so an id() key eventually returns
# the previous video's track. A WeakKeyDictionary keys on identity safely and
# evicts entries when the framemeta is collected.
_TRACKS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


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

    track = build_place_track(
        framemeta,
        resolver if resolver is not None else PlaceResolver(),
        lang,
        target_m,
        settings.place_initial_samples,
        settings.place_max_lookups if max_lookups is None else max_lookups,
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

    @allow_attributes({
        "x", "y", "size", "align", "rgb", "outline", "outline_width",
        "direction", "lang", "cache",
    })
    def create_place(self, element, entry, resolver=None, **kwargs):
        lang = attrib(element, "lang", d="en")
        target_m = settings.place_target_metres
        track = track_for(self.framemeta, lang, target_m, resolver=resolver)

        def value() -> str:
            """Always a str - CachingText raises ValueError on None (C6)."""
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
```

- [ ] **Step 4: Register the patch**

In `src/gpstitch/patches/__init__.py`, add to the imports inside `apply_patches()`:

```python
    from gpstitch.patches.place_patches import patch_place_widget
```

and call it after `patch_metric_accessor()`:

```python
    patch_place_widget()
```

Then add this bullet to the module docstring's list (lines 1-9), which enumerates every patch:

```
- Place name overlay widget (`create_place` component, always applied)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/patches/test_place_patches.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Verify the full suite and the patch log line**

Run: `uv run pytest -m "not e2e" -q`
Expected: all pass.

Run: `uv run gpstitch-dashboard --help 2>&1 | head -3`
Expected: still reports `Patches applied successfully`, no traceback.

- [ ] **Step 7: Commit**

```bash
git add src/gpstitch/patches/place_patches.py src/gpstitch/patches/__init__.py tests/unit/patches/test_place_patches.py
git commit -m "Add create_place overlay widget"
```

---

### Task 8: Editor metadata and documentation

**Files:**
- Modify: `src/gpstitch/services/widget_registry.py:178` (immediately after the `text` entry)
- Modify: `README.md:240` (Configuration), `:256` (Runtime Patches), `:323` (Acknowledgments), `:13` (Features)
- Test: `tests/unit/services/test_widget_registry.py`

**Interfaces:**
- Consumes: nothing at runtime.
- Produces: `widget_registry.get_metadata("place")` returning a `WidgetMetadata` with `type="place"`, `category=WidgetCategory.TEXT`.

No JavaScript changes: the palette is served from `/api/editor`, and the frontend `WIDGETS_WITH_SIZE_AS_BOX` sets exclude text-like widgets by omission.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/services/test_widget_registry.py`:

```python
class TestPlaceWidget:
    """The place-name overlay widget (spec 2026-09-04)."""

    def test_place_widget_is_registered(self):
        from gpstitch.services.widget_registry import widget_registry

        meta = widget_registry.get_metadata("place")
        assert meta is not None
        assert meta.type == "place"

    def test_place_is_a_text_category_widget(self):
        from gpstitch.models.editor import WidgetCategory
        from gpstitch.services.widget_registry import widget_registry

        assert widget_registry.get_metadata("place").category == WidgetCategory.TEXT

    def test_place_exposes_position_and_text_styling(self):
        from gpstitch.services.widget_registry import widget_registry

        names = {p.name for p in widget_registry.get_metadata("place").properties}
        assert {"x", "y", "size", "rgb", "outline", "outline_width", "align"} <= names

    def test_place_exposes_a_language_property_defaulting_to_english(self):
        from gpstitch.services.widget_registry import widget_registry

        lang = next(p for p in widget_registry.get_metadata("place").properties if p.name == "lang")
        assert lang.constraints.default == "en"

    def test_place_description_carries_data_attribution(self):
        """Spec D6: attribution surfaces in the property panel."""
        from gpstitch.services.widget_registry import widget_registry

        desc = widget_registry.get_metadata("place").description
        assert "OpenStreetMap" in desc and "GeoNames" in desc
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/services/test_widget_registry.py::TestPlaceWidget -v`
Expected: FAIL — `get_metadata("place")` returns `None`

- [ ] **Step 3: Write minimal implementation**

In `src/gpstitch/services/widget_registry.py`, immediately after the `self._metadata["text"] = WidgetMetadata(...)` block closes and before the `# METRIC` comment, insert:

```python
        # PLACE NAME
        self._metadata["place"] = WidgetMetadata(
            type="place",
            name="Place Name",
            description=(
                "Name of the town, village or city at the current GPS position. "
                "Place data (c) OpenStreetMap contributors (ODbL 1.0) and GeoNames (CC-BY 4.0)."
            ),
            category=WidgetCategory.TEXT,
            icon="P",
            default_width=200,
            default_height=30,
            properties=_common_position_props()
            + [
                PropertyDefinition(
                    name="lang",
                    label="Name Language",
                    type=PropertyType.SELECT,
                    description=(
                        "Language for place names. Applies to online lookups only - "
                        "the offline dataset ships local names, not translations."
                    ),
                    options=[
                        SelectOption(value="en", label="English"),
                        SelectOption(value="de", label="German"),
                        SelectOption(value="fr", label="French"),
                        SelectOption(value="es", label="Spanish"),
                        SelectOption(value="it", label="Italian"),
                        SelectOption(value="nl", label="Dutch"),
                        SelectOption(value="hu", label="Hungarian"),
                        SelectOption(value="ru", label="Russian"),
                    ],
                    constraints=PropertyConstraints(default="en"),
                    category="Content",
                ),
            ]
            + _common_text_props()
            + [
                PropertyDefinition(
                    name="direction",
                    label="Direction",
                    type=PropertyType.SELECT,
                    options=[
                        SelectOption(value="ltr", label="Left to Right"),
                        SelectOption(value="ttb", label="Top to Bottom"),
                    ],
                    constraints=PropertyConstraints(default="ltr"),
                    category="Appearance",
                ),
            ],
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/services/test_widget_registry.py -v`
Expected: PASS, including the five new tests.

- [ ] **Step 5: Verify it reaches the API**

Run: `uv run python -c "
from gpstitch.services.widget_registry import widget_registry
m = widget_registry.get_metadata('place')
print(m.type, '|', m.category, '|', len(m.properties), 'properties')
"`
Expected: `place | WidgetCategory.TEXT | 11 properties` (2 position + 1 lang + 5 text + 1 direction = 9; the exact count may differ, confirm it is non-zero and includes `lang`).

- [ ] **Step 6: Update the README**

Add to the Features list (around line 13):

```markdown
- **Place Name Overlay** — Shows the town, village or city at the current GPS position, resolved from OpenStreetMap with an offline fallback
```

Add to the Configuration table (around line 240):

```markdown
| `PLACE_ENABLE_NETWORK`  | `true`                  | Use online geocoding; `false` forces offline data      |
| `PLACE_TARGET_METRES`   | `500`                   | Sampling resolution for place-name transitions         |
| `PLACE_MAX_LOOKUPS`     | `400`                   | Hard cap on geocoding calls per render                 |
| `PLACE_USER_AGENT`      | `GPStitch (+…)`         | Identifies the app to Nominatim, as its policy requires |
```

Add to the Runtime Patches list (around line 256):

```markdown
- **Place name widget** — Adds a `place` component that renders the settlement name at the current GPS position
```

Add to Acknowledgments (around line 323):

```markdown
### Place data

Place names come from [OpenStreetMap](https://www.openstreetmap.org/) via
[Nominatim](https://nominatim.openstreetmap.org/), licensed under the
[ODbL 1.0](https://opendatacommons.org/licenses/odbl/), and from
[GeoNames](https://www.geonames.org/), licensed
[CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/).
```

- [ ] **Step 7: Full verification**

Run: `uv run ruff check src tests && uv run ruff format src tests && uv run pytest -m "not e2e" -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/gpstitch/services/widget_registry.py tests/unit/services/test_widget_registry.py README.md
git commit -m "Register the place name widget and document its data sources"
```

---

### Task 9: Bound the preview lookup budget (C13)

**Files:**
- Modify: `src/gpstitch/patches/place_patches.py`
- Modify: `src/gpstitch/services/renderer.py:1318`, `:1517`
- Test: `tests/unit/patches/test_place_patches.py` (append)

**Interfaces:**
- Consumes: `track_for` (Task 7), `settings.place_preview_max_lookups` (Task 1).
- Produces: `preview_budget()` — a context manager that makes `track_for` use the
  smaller preview budget for calls made inside it.

Preview blocks on the network by design (spec D5), and it runs on
`_executor = ThreadPoolExecutor(max_workers=2)` behind a debouncer. Without a
smaller budget, the first preview of a long uncached track can occupy both
workers for minutes, freezing the editor. Because every refinement level is
complete in itself (D7), the coarse answer preview gets is correct — just less
precise at transitions.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/patches/test_place_patches.py`:

```python
class TestPreviewBudget:
    """C13: preview gets a smaller lookup budget than render."""

    def test_preview_budget_limits_lookups(self, monkeypatch):
        from gpstitch.config import settings
        from gpstitch.patches import place_patches

        monkeypatch.setattr(settings, "place_preview_max_lookups", 3)
        monkeypatch.setattr(settings, "place_initial_samples", 50)
        place_patches._TRACKS.clear()

        r = StubResolver()
        with place_patches.preview_budget():
            place_patches.track_for(FakeFrameMeta(n=100), "en", 500, resolver=r)
        assert r.calls <= 3

    def test_render_uses_the_full_budget(self, monkeypatch):
        from gpstitch.config import settings
        from gpstitch.patches import place_patches

        monkeypatch.setattr(settings, "place_preview_max_lookups", 3)
        monkeypatch.setattr(settings, "place_initial_samples", 11)
        monkeypatch.setattr(settings, "place_max_lookups", 400)
        place_patches._TRACKS.clear()

        r = StubResolver()
        place_patches.track_for(FakeFrameMeta(n=100), "en", 500, resolver=r)
        assert r.calls == 11

    def test_budget_is_restored_after_the_block(self, monkeypatch):
        from gpstitch.patches import place_patches

        with place_patches.preview_budget():
            pass
        assert place_patches._preview_active() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/patches/test_place_patches.py::TestPreviewBudget -v`
Expected: FAIL — `AttributeError: module 'gpstitch.patches.place_patches' has no attribute 'preview_budget'`

- [ ] **Step 3: Write minimal implementation**

In `src/gpstitch/patches/place_patches.py`, add near the top:

```python
import contextlib
import threading

# Thread-local because preview runs on a ThreadPoolExecutor while a render may
# be in flight; a global flag would leak one context into the other.
_state = threading.local()


def _preview_active() -> bool:
    return getattr(_state, "preview", False)


@contextlib.contextmanager
def preview_budget():
    """Use the smaller preview lookup budget inside this block (C13)."""
    previous = _preview_active()
    _state.preview = True
    try:
        yield
    finally:
        _state.preview = previous
```

Then in `track_for`, replace the `max_lookups` resolution:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/patches/test_place_patches.py -v`
Expected: PASS

- [ ] **Step 5: Wire the preview call sites**

In `src/gpstitch/services/renderer.py`, both preview paths build widgets through
`layout_from_xml`. Wrap each so the widgets created inside use the preview budget.

At the top of the file, with the other first-party imports:

```python
from gpstitch.patches.place_patches import preview_budget
```

At `renderer.py:1318`, change:

```python
        create_widgets = layout_from_xml(
```

to wrap the call and the `Overlay` construction that follows it:

```python
        with preview_budget():
            create_widgets = layout_from_xml(
```

(indent the continuation lines and the `overlay = Overlay(framemeta, create_widgets)`
line at `:1328` to sit inside the `with` block — widget creation is where the
track is built, so the `Overlay` call must be inside it.)

Apply the identical change at `renderer.py:1517` in `_render_layout_with_data`.

- [ ] **Step 6: Verify the wiring**

Run: `uv run pytest -m "not e2e" -q`
Expected: all pass.

Run: `uv run ruff check src tests && uv run ruff format src tests`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/gpstitch/patches/place_patches.py src/gpstitch/services/renderer.py tests/unit/patches/test_place_patches.py
git commit -m "Bound the place lookup budget during preview"
```

---

## Manual verification

After Task 8, confirm the feature end-to-end. This is not covered by tests, because tests never touch the network.

1. Start the app: `uv run gpstitch --reload`
2. Open the layout editor and confirm **Place Name** appears under the Text category in the widget palette.
3. Drag it onto the canvas; confirm the properties panel shows position, font size, colour, outline, alignment and Name Language, and that the description mentions OpenStreetMap and GeoNames.
4. Load `tests/fixtures/videos/hiking_activity.gpx` as the primary file and confirm the preview renders a name. That fixture is in the Khibiny mountains, so the expected text is the widened county — `Kirovsk Urban Okrug` in English — not a village.
5. Set Name Language to Russian and confirm the text changes to Cyrillic. If it renders as empty boxes, the font lacks Cyrillic glyphs — expected, and the reason English is the default.
6. Confirm the second preview is instant (cache warm).
7. Render with the default ffmpeg profile and confirm the name appears in the output video.
8. Confirm offline behaviour: set `GPSTITCH_PLACE_ENABLE_NETWORK=false`, delete `~/.gopro-graphics/placecache.sqlite`, and confirm a name still renders from the bundled dataset.
