# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync --all-extras                        # install (Python 3.14, pinned in .python-version)
uv run gpstitch                             # run the app on :8000, opens a browser

uv run ruff check src tests                 # CI runs these two
uv run ruff format --check src tests

uv run pytest -m "not e2e"                  # what CI runs; ~30s
uv run pytest tests/unit tests/api -q       # the fast inner loop
uv run pytest tests/e2e -q                  # ~4 min, Playwright (uv run playwright install chromium)
uv run pytest tests/unit/services/test_renderer.py::TestClassName::test_name
```

`tests/integration` is marked `integration` and needs the fixture videos in
`tests/fixtures/videos/`. E2E starts its own uvicorn on an ephemeral port, so it
never collides with a server you already have running.

Static assets under `src/gpstitch/static/` are served from disk — an HTML/JS/CSS
change is live on the next page load, no restart. Python changes need a restart.

## Architecture

GPStitch is a FastAPI + vanilla-JS front end over the `gopro-overlay` library.
The library is never forked; it is monkey-patched at runtime from
`src/gpstitch/patches/`.

**The library is used two different ways, and most confusing behaviour comes
from being in the wrong one:**

- **Preview** runs in-process — `services/renderer.py:render_preview` builds a
  layout and returns a PNG.
- **Render** shells out — `services/render_service.py:start_render` gets a
  command string from `renderer.generate_cli_command`, then runs
  `scripts/gopro_dashboard_wrapper.py` (the `gpstitch-dashboard` console script),
  which applies patches and execs the real `gopro-dashboard.py`.

Patches come in two tiers. `patches/apply_patches()` runs at import in both
processes (ffmpeg options, DJI metrics, the `place` widget). `gpx_patches` and
`odo_patches` are applied **only by the wrapper**, and only when it sees one of
GPStitch's own flags: `--ts-srt-source`, `--ts-dji-meta-source`,
`--ts-odo-offset`, `--ts-srt-video`. The wrapper strips them before exec, since
the real CLI would reject them. A feature that works in preview but not in
render is usually a patch that only one side applied.

`generate_cli_command` has four modes, chosen from the session's files:
1. video only (GoPro embedded GPS), 2. video + GPX/FIT, 3. overlay-only (no
video), 4. DJI Action with embedded GPS — which extracts the protobuf stream to
a temp GPX and renders with `--use-gpx-only`.

A merged batch is mode 2 in disguise: the clips are joined into one file during
the job's preparation (`services/merge_preparation.py`) and rendered against a
GPX combining every clip's GPS, because ffmpeg cannot carry the DJI telemetry
stream through a concat.

**Request flow for a render:** `api/render.py` creates a session in
`services/file_manager.py`, a job in `services/job_manager.py`, then hands off to
`render_service`. Jobs persist as JSON under `$TMPDIR/gpstitch/jobs/` and carry
their full log — read that file directly when diagnosing a failed render, the
`error` field is usually more informative than what surfaces in the UI. A job
records the pid that created it so a second process cannot declare another's
render dead.

**Layouts** resolve to an XML path before the CLI sees them: a custom template
from `~/.gpstitch/templates/`, else `src/gpstitch/layouts/<name>.xml`, else
gopro-overlay's own built-in. A custom template stores its canvas size in a
sidecar `{name}.json`, and that wins for `--overlay-size` — widget coordinates
are pixel-absolute, so rendering a template onto a different canvas shifts
everything.

## Constraints in gopro-overlay worth knowing

- `Timeseries` is keyed by datetime — points sharing a timestamp collapse to one
  entry, so a track whose clock never advances cannot be rendered at all.
- `--layout` accepts only `default`, `speed-awareness`, `xml`.
- `--exclude`/`--include` match only components with a `name` attribute, which
  editor-made templates do not have; remove components by filtering the XML
  (`services/layout_filter.py`).
- `Timeunit` has `.millis()`, not `.total_seconds()`.
- `Entry.interpolate` subtracts every field, so a string on an `Entry` raises
  `TypeError` per frame. Place names live in a side table
  (`services/place_track.py`) for this reason.
- `find_recording()` runs even under `--use-gpx-only`, which is where "Unable to
  locate metadata stream - is it a GoPro file" comes from.

## DJI embedded GPS

`services/dji_meta_parser.py` parses the protobuf `djmd` stream from Osmo Action
cameras. Extraction is seek-bound across the whole file — ~107s on a 12GB clip
regardless of how few points you want — so **prefer a bounded read**:
`first_dji_meta_point()`, `parse_dji_meta_window()` or `sample_dji_meta_track()`
over `parse_dji_meta_file()`. Reading the entire stream to obtain one small value
has been introduced and removed three separate times; it hides well, because two
stages each costing 107s just look like "loading is slow". Time stages
individually before optimising, and measure end-to-end after.

The GPS remote can drop its link and re-report a stale fix, so the parser also
repairs telemetry: `clock_unreliable()` spots a long run of one timestamp and
`rebuild_timestamps()` re-derives the axis from the sample index at the stream's
declared rate; `position_frozen()` spots a fix that never moves, and the layout
then loses the widgets that need movement.

## Frontend

`src/gpstitch/static/unified/` is the live UI. No bundler and no ES modules:
each file ends with `window.Thing = Thing` and `index.html` loads all ~28 with
plain `<script src>` tags in dependency order — a new component must be added to
that list. `static/editor/` is the previous UI, still served at `/legacy`; leave
it alone.

`window.toast.{info,success,warning,error}(msg, {title, duration})` exists and
`UnifiedApp.js` uses it, but several older components still call `alert()` —
match the file you are in. There is no `window.showToast`.

The right panel's Config tab is present in both Quick and Advanced mode, and is
where settings shared across both live. Asserting on `textContent` will not
catch CSS faults; screenshot the page for anything layout-related.
