# Place Name Overlay — Design

**Date:** 2026-09-04
**Status:** Approved for planning
**Branch:** `feat/place-name-overlay`

## Goal

Add a configurable overlay widget that displays the name of the settlement the GPS
position is currently in — "Giethoorn", "Bath" — so a viewer can tell where the
footage was shot. Position and appearance are configurable like any other text
widget.

The smallest scope is a village or city. Street and road names are never shown.

## Decisions

These were settled during brainstorming and the deep review rounds. Each is binding
on the implementation.

| # | Decision | Rationale |
|---|---|---|
| D1 | Online Nominatim **with** offline fallback | Best coverage; user accepted the two-path cost |
| D2 | Show the name; widen up the hierarchy when there is no settlement | Text is always present rather than blinking out |
| D3 | Language configurable, default English | Nominatim returns local script by default; Cyrillic/CJK would break the font |
| D4 | Skip `hamlet`; start the chain at `village` | "Giethoorn" is what a person says, not "Klooster" |
| D5 | Preview blocks until resolved | User's explicit choice over cache-only |
| D6 | Attribution in README + widget property-panel note | Meets ODbL/CC-BY without forcing text onto the video |
| D7 | Progressive refinement, coarse to fine | Anytime coverage; a failure leaves a complete coarser track |
| D8 | Refine only intervals whose endpoints disagree | ~39 calls instead of ~100, and transitions land more precisely |
| D9 | Offline dataset is GeoNames `cities500`, pre-trimmed | Only floor low enough to contain villages (D4) |
| D10 | Target resolution 500 m | User confirmed well within acceptable accuracy |

## Architecture

Three new units, each independently testable.

```
   framemeta (entries: dt, point.lat/lon, codo)
        │
        ▼
   place_track.build()            ← progressive refinement (D7, D8)
        │   asks for lat/lon → name
        ▼
   PlaceResolver.resolve()        ← one interface, two backends
        ├── SqliteDict cache       ~/.gopro-graphics/placecache.sqlite
        ├── NominatimBackend       online, rate-limited, ODbL
        └── CitiesBackend          offline, bundled cities500, CC-BY
        │
        ▼
   PlaceTrack  [(dt, PlaceName, backend)]
        │   bisect: last sample at or before dt
        ▼
   create_place → CachingText(value=lambda: track.at(entry().dt))
```

### `services/place_resolver.py`

- `PlaceName` — the resolved hierarchy (`village`, `town`, `city`, `municipality`,
  `county`, `state`, `country`), **not** a flattened string. Widening (D2) is a
  display concern applied at draw time, so the cache stays reusable across
  different display settings.
- `PlaceResolver.resolve(lat, lon, lang) -> tuple[PlaceName | None, Backend]`.
  Returns which backend answered — required by C3.
- Cache key `(round(lat,3), round(lon,3), lang)`, ~110 m buckets.
- `NominatimBackend` — `zoom=14` (no `road` key at that zoom, satisfying the
  no-street-names rule structurally rather than by filtering), `accept-language`,
  identifying User-Agent `GPStitch/<version>`, cross-process rate limit (C1).
- `CitiesBackend` — lazily loaded (C5), latitude-band nearest neighbour.

### `services/place_track.py`

Builds the track by progressive refinement:

1. **Level 0** — 11 samples at 0%,10%,…,100% of cumulative distance (`codo`).
   Whole trip covered after ~11 calls.
2. **Refine** — for each adjacent pair whose names *differ* and whose spacing
   exceeds 500 m, resolve the midpoint. Repeat until every differing interval is
   under 500 m or the call budget is spent.
3. Each level completes atomically. Abandoning at any point leaves a complete,
   coarser track.

Lookup is `bisect` for the last sample at or before a given `dt`.

### `patches/place_patches.py`

Adds `create_place` to `layout_xml.Widgets`, following `metric_patches.py`
(idempotency flag, registered in `patches/__init__.py`).

```xml
<component type="place" x="120" y="60" size="32" lang="en"/>
```

Track construction is memoised on `(id(framemeta), lang, target_m)` — see C4.

## Constraints

Discovered by tracing the design against the codebase. Violating any of these
reintroduces a specific, identified bug.

- **C1 — Rate limiting must be cross-process.** Preview (web app) and render
  (subprocess) are separate processes. Two in-process limiters yield 2 req/s
  against Nominatim's 1 req/s cap. Use a lock file in `gopro_config_dir`.
- **C2 — Place names must never be attached to `Entry`.** `Entry.interpolate`
  computes `end - start` for every key and catches only `KeyError`. A string field
  raises `TypeError`. `FrameMeta.get` interpolates via `_get_closest`, so this
  would fire on nearly every frame. The separate track is mandatory, not stylistic.
- **C3 — Only compare samples from the same backend.** Disagreement-gating (D8)
  against a mixed-backend track invents phantom boundaries, because the two
  backends word results differently (C7). When the backend changes mid-run, stop
  refining rather than compare across sources.
- **C4 — Do not memoise on `framemeta` identity alone.** Preview rebuilds framemeta
  every time (`renderer.py:1303`, `:1315`), so identity-only keys always miss, and
  two `place` widgets with different `lang` would collide.
- **C5 — Load the cities dataset lazily.** Only when a layout contains a `place`
  widget, so unrelated `gpstitch-dashboard` runs pay nothing.
- **C6 — The value callable returns `str`, never `None`.** `CachingText` raises
  `ValueError` on `None`. Terminal case is `""`. Applies to: `entry()` being `None`
  before the first draw, `entry().point` being `None` (no GPS fix), open ocean with
  no country, and total resolution failure.
- **C7 — Offline cannot produce the full chain.** GeoNames stores `admin1`/`admin2`
  as codes, not names. Bundling `admin1CodesASCII.txt` (0.1 MB) gives
  village → state → country. Municipality and county are online-only.
- **C8 — Cache writes must never fail a render.** `SqliteDict(autocommit=True)`
  can raise "database is locked" under multi-process contention. Wrap and log.
- **C9 — `codo` is a pint `Quantity` and may be `None`.** Use unit-aware arithmetic
  or `.magnitude`; fall back to time-based subdivision when distance is absent.
- **C10 — Hard call budget.** A track where every sample differs degenerates to
  uniform refinement. Cap total lookups.
- **C11 — Empty framemeta.** `FrameMeta.min`/`max` fail on zero entries. Guard to
  an empty track.
- **C12 — `lang` (D3) is honoured online only.** GeoNames ships `name` and
  `asciiname`, not translations, so the offline backend cannot satisfy a non-English
  `lang`. When it answers, it returns the local/ASCII name regardless of the
  setting. The property-panel help text must say so, or users will read the
  difference as a bug.
- **C13 — Preview and render get separate call budgets.** D5 blocks preview on the
  network, so an uncached long track would otherwise freeze the editor for minutes
  on both `_executor` workers. Preview stops after a small budget (level 0 plus a
  few refinements) and shows the coarse answer; render uses the full budget from
  C10. Because D7 completes each level atomically, the coarse preview answer is
  correct, just less precise at transitions.

## Verified — no changes required

Confirmed by inspection during review; recorded so they are not re-litigated.

- `xml_to_layout` is type-agnostic — `place` round-trips.
- `elem.text` → `value` mapping is guarded to `text`/`metric_unit`
  (`xml_converter.py:215`).
- `place` is absent from `WIDGETS_WITHOUT_XY` / `WITH_SIZE`, so it gets `x,y`
  like `text`.
- Frontend `WIDGETS_WITH_SIZE_AS_BOX` sets exclude text-like widgets by omission —
  **no JavaScript changes**.
- `test_widget_registry.py:61` asserts `> 20`, a lower bound.
- Templates store `EditorLayout` JSON generically.
- The wheel already ships 49 non-`.py` files, so bundled data needs no packaging
  changes.
- GPStitch performs no component-type validation; `hasattr(factory, attr)` in
  `layout_xml.py` is the only gate.
- Batch / shared-GPX passes only `odo_offset` and is layout-agnostic.
- `_render_layout_placeholder` never calls `layout_from_xml`, so the no-file
  preview is unaffected.

## Measured

A 200k-row synthetic dataset, trimmed to six columns:

| Metric | Result |
|---|---|
| Gzipped size | 2.3 MB |
| Load + sort | 0.21 s (once, lazy) |
| 39 lookups | 4.5 ms total |
| Candidates scanned per lookup | 1,507 of 200,000 |

Pure Python is sufficient. `reverse_geocoder` was rejected: it requires numpy,
scipy, and a C extension built at install time.

## Changes

**New**

| Path | Purpose |
|---|---|
| `src/gpstitch/services/place_resolver.py` | Resolver, backends, cache, rate limit |
| `src/gpstitch/services/place_track.py` | Progressive refinement, bisect lookup |
| `src/gpstitch/patches/place_patches.py` | `create_place` on `Widgets` |
| `src/gpstitch/data/cities500.tsv.gz` | Trimmed GeoNames dataset (~2.3 MB) |
| `src/gpstitch/data/admin1.tsv` | Admin-1 code → name (~0.1 MB) |
| `scripts/build_places_dataset.py` | Regenerates the trimmed data; records source and date |
| `tests/unit/services/test_place_resolver.py` | |
| `tests/unit/services/test_place_track.py` | |
| `tests/unit/patches/test_place_patches.py` | |

**Modified**

| Path | Change |
|---|---|
| `src/gpstitch/patches/__init__.py:31-36` | Register `patch_place_widget()` |
| `src/gpstitch/patches/__init__.py:1-9` | Docstring enumerates every patch |
| `src/gpstitch/services/widget_registry.py:145` | `place` metadata after `text` |
| `src/gpstitch/config.py:45` | `place_*` settings |
| `pyproject.toml:29` | Declare `requests` (currently only transitive) |
| `README.md:240` | Config table rows |
| `README.md:256` | Runtime Patches list |
| `README.md:323` | OSM / GeoNames attribution |

## Testing

All offline; backends stubbed; no network in CI; a tiny synthetic cities fixture.

- Widening chain: village → town → city → municipality → county → state → country → `""`
- Cache hit/miss, key rounding, language passthrough
- Backend failover on network error, timeout, HTTP 429
- Refinement: identical samples → no refinement; one boundary → bisects toward it;
  all-different → stops at the budget; track under 500 m → level 0 only
- C3: a mixed-backend track does not manufacture a boundary
- `track.at()` before first sample, between samples, after last
- C6 cases each return `""`
- XML attribute parsing and the `allow_attributes` whitelist
- Integration: XML → rendered frame containing the expected string

## Out of scope

- On-video attribution text (D6 chose docs + property panel)
- Boundary precision beyond 500 m
- Any geocoding provider other than Nominatim
- Per-place styling, icons, or flags
- Caching place data inside project templates
