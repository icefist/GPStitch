# Merged Batch Render Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A "Merge into one video" toggle on batch render that produces one rendered output from several clips, driven by one continuous GPS track, so the journey map no longer restarts at every file boundary.

**Architecture:** The batch endpoint creates a single render job carrying the list of source clips. That job's existing preparation phase joins the clips losslessly with ffmpeg's concat demuxer, builds one GPX whose points sit on the joined timeline, and registers the joined file as the session's primary video with that GPX as its secondary. Rendering is then completely unchanged.

**Tech Stack:** Python 3.14, FastAPI, pydantic v2, pytest (asyncio_mode=auto), Playwright for e2e, ffmpeg/ffprobe, vanilla JS front end with `window.X` globals.

**Spec:** `docs/superpowers/specs/2026-09-16-merged-batch-render-design.md`

## Global Constraints

- Run tests with `.venv/bin/python -m pytest`, lint with `.venv/bin/python -m ruff check src tests` and `.venv/bin/python -m ruff format src tests`. Both must pass before every commit.
- ruff: line-length 120, double quotes, target py312, selects E/W/F/I/B/C4/UP/SIM.
- C1. Grouping is "whatever is selected" — no auto-detection of rides.
- C2. The joined source file is deleted once the render succeeds.
- C3. One selected file with merge on renders normally — no join.
- C4. A shared GPX wins over embedded GPS; when one is supplied, GPS extraction is skipped.
- C5. Mixed resolution or video codec is refused before anything is written.
- C6. Insufficient disk is refused before anything is written, naming bytes required and free.
- C7. Cancellation kills the join subprocess.
- C8. Output name is the first clip's stem + `_merged_overlay` + the profile's extension.
- C9. Pre-check reports one planned output when merge is on.
- C10. Position widgets are dropped only when the whole track never moves (existing behaviour; no change needed).
- C11. Preview stays per-clip (no change needed).
- The front end has no bundler: a new JS file must be added to the `<script src>` list in `src/gpstitch/static/unified/index.html`.

---

### Task 1: Clip compatibility and disk preflight

**Files:**
- Create: `src/gpstitch/services/video_merge.py`
- Test: `tests/unit/services/test_video_merge.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `MergeNotPossible(Exception)`; `ClipProfile(width: int, height: int, video_codec: str)`; `probe_clip(path: Path) -> ClipProfile`; `check_mergeable(paths: list[Path], scratch_dir: Path) -> int` returning the total source size in bytes.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/services/test_video_merge.py`:

```python
"""Refusing a join that would produce garbage, before anything is written.

Concatenating clips whose video streams differ decodes into rubbish after the
first boundary, and a join needs as much free space as the clips occupy. Both
are cheap to check and expensive to discover after an 18-minute copy.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from gpstitch.services.video_merge import (
    ClipProfile,
    MergeNotPossible,
    check_mergeable,
)


def _clips(tmp_path, count=2, size=1024):
    paths = []
    for i in range(count):
        p = tmp_path / f"clip{i}.mp4"
        p.write_bytes(b"\0" * size)
        paths.append(p)
    return paths


class TestCheckMergeable:
    def test_matching_clips_return_their_total_size(self, tmp_path):
        paths = _clips(tmp_path, count=2, size=1024)
        profile = ClipProfile(width=2688, height=1512, video_codec="hevc")

        with patch("gpstitch.services.video_merge.probe_clip", return_value=profile):
            total = check_mergeable(paths, scratch_dir=tmp_path)

        assert total == 2048

    def test_a_different_resolution_is_refused(self, tmp_path):
        paths = _clips(tmp_path)
        profiles = [
            ClipProfile(width=2688, height=1512, video_codec="hevc"),
            ClipProfile(width=1920, height=1080, video_codec="hevc"),
        ]

        with (
            patch("gpstitch.services.video_merge.probe_clip", side_effect=profiles),
            pytest.raises(MergeNotPossible, match="resolution"),
        ):
            check_mergeable(paths, scratch_dir=tmp_path)

    def test_a_different_codec_is_refused(self, tmp_path):
        paths = _clips(tmp_path)
        profiles = [
            ClipProfile(width=2688, height=1512, video_codec="hevc"),
            ClipProfile(width=2688, height=1512, video_codec="h264"),
        ]

        with (
            patch("gpstitch.services.video_merge.probe_clip", side_effect=profiles),
            pytest.raises(MergeNotPossible, match="codec"),
        ):
            check_mergeable(paths, scratch_dir=tmp_path)

    def test_the_refusal_names_both_clips(self, tmp_path):
        """"They differ" is useless when the batch holds twenty files."""
        paths = _clips(tmp_path)
        profiles = [
            ClipProfile(width=2688, height=1512, video_codec="hevc"),
            ClipProfile(width=1920, height=1080, video_codec="hevc"),
        ]

        with (
            patch("gpstitch.services.video_merge.probe_clip", side_effect=profiles),
            pytest.raises(MergeNotPossible) as caught,
        ):
            check_mergeable(paths, scratch_dir=tmp_path)

        assert "clip1.mp4" in str(caught.value)
        assert "2688x1512" in str(caught.value)
        assert "1920x1080" in str(caught.value)

    def test_insufficient_disk_is_refused(self, tmp_path):
        paths = _clips(tmp_path, count=2, size=1024)
        profile = ClipProfile(width=2688, height=1512, video_codec="hevc")

        class Usage:
            free = 512

        with (
            patch("gpstitch.services.video_merge.probe_clip", return_value=profile),
            patch("gpstitch.services.video_merge.shutil.disk_usage", return_value=Usage()),
            pytest.raises(MergeNotPossible, match="disk"),
        ):
            check_mergeable(paths, scratch_dir=tmp_path)

    def test_fewer_than_two_clips_is_refused(self, tmp_path):
        """A single clip needs no join; callers must not reach here with one."""
        with pytest.raises(MergeNotPossible, match="two"):
            check_mergeable(_clips(tmp_path, count=1), scratch_dir=tmp_path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/services/test_video_merge.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'gpstitch.services.video_merge'`

- [ ] **Step 3: Write the implementation**

Create `src/gpstitch/services/video_merge.py`:

```python
"""Join several clips into one file, losslessly.

A DJI Osmo Action splits a long ride into ~17GB files. Rendered separately, each
gets its own journey map, so the joins show. Joining the clips first gives the
renderer one continuous video and one continuous track.

The join is a stream copy - no re-encode - so it is bound by disk speed rather
than CPU, and it cannot change the picture.
"""

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from gopro_overlay.ffmpeg import FFMPEG

logger = logging.getLogger(__name__)

# Room for the container's own overhead on top of the copied streams.
_HEADROOM_BYTES = 256 * 1024 * 1024


class MergeNotPossible(Exception):
    """The selected clips cannot be joined. The message says why."""


@dataclass(frozen=True)
class ClipProfile:
    """The parts of a clip that have to match for a stream copy to be valid."""

    width: int
    height: int
    video_codec: str

    def describe(self) -> str:
        return f"{self.width}x{self.height} {self.video_codec}"


def probe_clip(path: Path) -> ClipProfile:
    """Read the video stream's dimensions and codec."""
    command = [
        FFMPEG().binary.replace("ffmpeg", "ffprobe"),
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,codec_name",
        "-of", "json",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise MergeNotPossible(f"Could not read {path.name}: {result.stderr.strip()}")

    streams = json.loads(result.stdout or "{}").get("streams") or []
    if not streams:
        raise MergeNotPossible(f"{path.name} has no video stream")

    stream = streams[0]
    return ClipProfile(
        width=int(stream["width"]),
        height=int(stream["height"]),
        video_codec=str(stream["codec_name"]),
    )


def check_mergeable(paths: list[Path], scratch_dir: Path) -> int:
    """Refuse a join that cannot work, before anything is written.

    Returns:
        Total size in bytes of the source clips.

    Raises:
        MergeNotPossible: clips differ in resolution or codec, or the scratch
            directory has too little room.
    """
    if len(paths) < 2:
        raise MergeNotPossible("Merging needs at least two clips")

    first_profile = probe_clip(paths[0])
    for path in paths[1:]:
        profile = probe_clip(path)
        if (profile.width, profile.height) != (first_profile.width, first_profile.height):
            raise MergeNotPossible(
                f"Clips differ in resolution: {paths[0].name} is {first_profile.describe()}, "
                f"{path.name} is {profile.describe()}. Joining them would produce a broken video."
            )
        if profile.video_codec != first_profile.video_codec:
            raise MergeNotPossible(
                f"Clips differ in codec: {paths[0].name} is {first_profile.describe()}, "
                f"{path.name} is {profile.describe()}. Joining them would produce a broken video."
            )

    total = sum(path.stat().st_size for path in paths)
    free = shutil.disk_usage(scratch_dir).free
    if free < total + _HEADROOM_BYTES:
        raise MergeNotPossible(
            f"Not enough disk to join these clips: {_gb(total + _HEADROOM_BYTES)} needed, "
            f"{_gb(free)} free in {scratch_dir}"
        )

    return total


def _gb(size_bytes: float) -> str:
    return f"{size_bytes / 1_000_000_000:.1f}GB"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/services/test_video_merge.py -q`
Expected: 6 passed

- [ ] **Step 5: Lint and commit**

```bash
.venv/bin/python -m ruff check src tests && .venv/bin/python -m ruff format src tests
git add src/gpstitch/services/video_merge.py tests/unit/services/test_video_merge.py
git commit -m "Refuse an unjoinable clip set before doing any work"
```

---

### Task 2: Joining the clips

**Files:**
- Modify: `src/gpstitch/services/video_merge.py`
- Test: `tests/unit/services/test_video_merge.py`
- Test: `tests/integration/test_video_merge_real.py`

**Interfaces:**
- Consumes: `MergeNotPossible` from Task 1.
- Produces: `write_concat_list(paths: list[Path], destination: Path) -> Path`; `join_clips(paths: list[Path], output: Path, on_progress: Callable[[str], None] | None = None, on_process: Callable[[subprocess.Popen], None] | None = None) -> Path`.

- [ ] **Step 1: Write the failing unit tests**

Append to `tests/unit/services/test_video_merge.py`:

```python
class TestConcatList:
    """ffmpeg's concat demuxer reads a list file with one `file '<path>'` per line."""

    def test_each_clip_gets_a_line(self, tmp_path):
        from gpstitch.services.video_merge import write_concat_list

        paths = _clips(tmp_path, count=2)
        listing = write_concat_list(paths, tmp_path / "list.txt")

        lines = listing.read_text(encoding="utf-8").strip().splitlines()
        assert lines == [f"file '{paths[0]}'", f"file '{paths[1]}'"]

    def test_a_quote_in_a_path_is_escaped(self, tmp_path):
        """An unescaped apostrophe ends the quoted string and ffmpeg misreads the path."""
        from gpstitch.services.video_merge import write_concat_list

        odd = tmp_path / "ride's clip.mp4"
        odd.write_bytes(b"\0")
        listing = write_concat_list([odd], tmp_path / "list.txt")

        assert listing.read_text(encoding="utf-8").strip() == f"file '{tmp_path}/ride'\\''s clip.mp4'"


class TestJoinClips:
    def test_the_ffmpeg_command_copies_streams(self, tmp_path):
        """A re-encode would take hours and change the picture."""
        from gpstitch.services.video_merge import join_clips

        captured = {}

        class FakeProcess:
            returncode = 0

            def communicate(self):
                return ("", "")

        def fake_popen(command, **kwargs):
            captured["command"] = command
            return FakeProcess()

        with patch("gpstitch.services.video_merge.subprocess.Popen", side_effect=fake_popen):
            join_clips(_clips(tmp_path), tmp_path / "out.mp4")

        command = captured["command"]
        assert "-c" in command and "copy" in command
        assert "concat" in command
        assert command[command.index("-map") + 1] == "0:v:0"

    def test_a_failed_join_raises_with_ffmpeg_stderr(self, tmp_path):
        from gpstitch.services.video_merge import MergeNotPossible, join_clips

        class FakeProcess:
            returncode = 1

            def communicate(self):
                return ("", "Invalid data found when processing input")

        with (
            patch("gpstitch.services.video_merge.subprocess.Popen", return_value=FakeProcess()),
            pytest.raises(MergeNotPossible, match="Invalid data"),
        ):
            join_clips(_clips(tmp_path), tmp_path / "out.mp4")

    def test_the_process_is_handed_to_the_caller(self, tmp_path):
        """Cancelling must be able to kill an 18-minute copy, not wait it out."""
        from gpstitch.services.video_merge import join_clips

        class FakeProcess:
            returncode = 0

            def communicate(self):
                return ("", "")

        handed = []
        with patch("gpstitch.services.video_merge.subprocess.Popen", return_value=FakeProcess()):
            join_clips(_clips(tmp_path), tmp_path / "out.mp4", on_process=handed.append)

        assert len(handed) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/services/test_video_merge.py -q -k "ConcatList or JoinClips"`
Expected: FAIL with `ImportError: cannot import name 'write_concat_list'`

- [ ] **Step 3: Write the implementation**

Append to `src/gpstitch/services/video_merge.py`:

```python
def write_concat_list(paths: list[Path], destination: Path) -> Path:
    """Write ffmpeg's concat list: one `file '<path>'` per line, in order.

    A literal apostrophe has to be escaped, or it closes the quoted string and
    ffmpeg reads a truncated path.
    """
    lines = [f"file '{str(path).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'" for path in paths]
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination


def join_clips(
    paths: list[Path],
    output: Path,
    on_progress: Callable[[str], None] | None = None,
    on_process: Callable[[subprocess.Popen], None] | None = None,
) -> Path:
    """Concatenate `paths` into `output` without re-encoding.

    Only video and audio are carried across: ffmpeg cannot remux the DJI `djmd`
    telemetry stream, so GPS comes from the original clips instead.

    `on_process` receives the running ffmpeg, so a cancelled job can kill a copy
    that would otherwise run for a quarter of an hour.
    """
    listing = write_concat_list(paths, output.parent / f"{output.stem}_concat.txt")

    command = [
        FFMPEG().binary,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-f", "concat",
        "-safe", "0",
        "-i", str(listing),
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c", "copy",
        str(output),
    ]

    if on_progress:
        on_progress(f"Joining {len(paths)} clips into one video - copying, not re-encoding")

    logger.info("Joining %d clips into %s", len(paths), output)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if on_process:
        on_process(process)
    _, stderr = process.communicate()

    if process.returncode != 0:
        raise MergeNotPossible(f"Joining the clips failed: {(stderr or '').strip()}")

    listing.unlink(missing_ok=True)
    if on_progress:
        on_progress("Clips joined")
    return output
```

Add to the imports at the top of the file:

```python
from collections.abc import Callable
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/services/test_video_merge.py -q`
Expected: 11 passed

- [ ] **Step 5: Write the integration test against real files**

Create `tests/integration/test_video_merge_real.py`:

```python
"""Joining real clips, to prove the stream copy actually works.

The unit tests assert the command; this asserts ffmpeg accepts it and that the
result is the sum of its parts.
"""

import subprocess
from pathlib import Path

import pytest

from gpstitch.services.video_merge import join_clips

FIXTURE = Path(__file__).parent.parent / "fixtures" / "videos" / "DJI_20260315180109_0003_D_5s_fixture.MP4"


def _duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


@pytest.mark.integration
def test_joining_a_clip_to_itself_doubles_its_duration(tmp_path):
    """The same file twice is a valid concat and the arithmetic is unambiguous."""
    output = tmp_path / "joined.mp4"

    join_clips([FIXTURE, FIXTURE], output)

    assert output.exists()
    assert _duration(output) == pytest.approx(_duration(FIXTURE) * 2, rel=0.02)


@pytest.mark.integration
def test_the_video_stream_survives_the_join(tmp_path):
    output = tmp_path / "joined.mp4"

    join_clips([FIXTURE, FIXTURE], output)

    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name", "-of", "csv=p=0", str(output)],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() != ""
```

- [ ] **Step 6: Run the integration tests**

Run: `.venv/bin/python -m pytest tests/integration/test_video_merge_real.py -q`
Expected: 2 passed

- [ ] **Step 7: Lint and commit**

```bash
.venv/bin/python -m ruff check src tests && .venv/bin/python -m ruff format src tests
git add src/gpstitch/services/video_merge.py tests/unit/services/test_video_merge.py tests/integration/test_video_merge_real.py
git commit -m "Join clips losslessly with ffmpeg's concat demuxer"
```

---

### Task 3: GPS on the joined timeline

**Files:**
- Modify: `src/gpstitch/services/dji_meta_parser.py`
- Test: `tests/unit/services/test_joined_timeline.py`

**Interfaces:**
- Consumes: `DjiMetaPoint` (existing, fields `frame_idx, timestamp, lat, lon, alt_m, velocity_2d`).
- Produces: `points_on_joined_timeline(segments: list[tuple[list[DjiMetaPoint], float]]) -> list[DjiMetaPoint]` where each tuple is one clip's points and that clip's duration in seconds, in play order.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/services/test_joined_timeline.py`:

```python
"""Placing several clips' GPS on the timeline of the joined video.

The joined video's timeline is the sum of the clip durations, so a clip's points
belong at the cumulative duration of everything before it - not at their own
wall-clock time. For auto-split parts the two are the same. For clips with a real
gap between them, the gap is compressed out, which is what keeps the overlay
matching the picture.
"""

from datetime import datetime, timedelta

import pytest

from gpstitch.services.dji_meta_parser import DjiMetaPoint, points_on_joined_timeline

START = datetime(2026, 9, 13, 14, 32, 6)


def clip(first_ts: datetime, count: int, hz: float = 30.0, start_idx: int = 0) -> list[DjiMetaPoint]:
    return [
        DjiMetaPoint(
            frame_idx=start_idx + i,
            timestamp=first_ts + timedelta(seconds=i / hz),
            lat=52.0 + i * 0.0001,
            lon=6.0,
            alt_m=10.0,
            velocity_2d=(1.0, 0.0),
        )
        for i in range(count)
    ]


class TestPointsOnJoinedTimeline:
    def test_a_single_clip_is_unchanged(self):
        points = clip(START, 10)

        joined = points_on_joined_timeline([(points, 10 / 30)])

        assert [p.timestamp for p in joined] == [p.timestamp for p in points]

    def test_the_second_clip_starts_where_the_first_ends(self):
        first = clip(START, 30)          # one second of samples
        second = clip(START + timedelta(seconds=1), 30)

        joined = points_on_joined_timeline([(first, 1.0), (second, 1.0)])

        assert joined[30].timestamp == START + timedelta(seconds=1)

    def test_a_gap_between_clips_is_compressed_out(self):
        """The joined video has no gap, so the track must not have one either."""
        first = clip(START, 30)
        second = clip(START + timedelta(minutes=15), 30)

        joined = points_on_joined_timeline([(first, 1.0), (second, 1.0)])

        assert joined[30].timestamp == START + timedelta(seconds=1)

    def test_timing_within_a_clip_is_preserved(self):
        first = clip(START, 30)
        second = clip(START + timedelta(minutes=15), 30)

        joined = points_on_joined_timeline([(first, 1.0), (second, 1.0)])

        offsets = [(p.timestamp - joined[30].timestamp).total_seconds() for p in joined[30:]]
        assert offsets[1] == pytest.approx(1 / 30, abs=1e-6)
        assert offsets[-1] == pytest.approx(29 / 30, abs=1e-6)

    def test_the_result_is_ordered(self):
        joined = points_on_joined_timeline([(clip(START, 30), 1.0), (clip(START, 30), 1.0)])

        assert joined == sorted(joined, key=lambda p: p.timestamp)

    def test_frame_indices_keep_increasing_across_clips(self):
        """Downstream code derives timing from frame_idx when a clock is stalled."""
        joined = points_on_joined_timeline([(clip(START, 30), 1.0), (clip(START, 30), 1.0)])

        indices = [p.frame_idx for p in joined]
        assert indices == sorted(indices)
        assert len(set(indices)) == len(indices)

    def test_gps_values_are_untouched(self):
        first = clip(START, 5)

        joined = points_on_joined_timeline([(first, 1.0)])

        assert [(p.lat, p.lon, p.alt_m) for p in joined] == [(p.lat, p.lon, p.alt_m) for p in first]

    def test_a_clip_with_no_points_leaves_a_hole_but_still_advances_time(self):
        """A clip whose GPS never locked must not shift the clips after it."""
        first = clip(START, 30)
        third = clip(START + timedelta(seconds=2), 30)

        joined = points_on_joined_timeline([(first, 1.0), ([], 1.0), (third, 1.0)])

        assert joined[30].timestamp == START + timedelta(seconds=2)

    def test_no_segments_gives_no_points(self):
        assert points_on_joined_timeline([]) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/services/test_joined_timeline.py -q`
Expected: FAIL with `ImportError: cannot import name 'points_on_joined_timeline'`

- [ ] **Step 3: Write the implementation**

Add to `src/gpstitch/services/dji_meta_parser.py`, after `derive_sample_rate`:

```python
def points_on_joined_timeline(
    segments: list[tuple[list[DjiMetaPoint], float]],
) -> list[DjiMetaPoint]:
    """Place several clips' GPS on the timeline of those clips joined end to end.

    Each segment is one clip's points and that clip's duration in seconds, in
    play order. A clip's points are re-based onto the cumulative duration of the
    clips before it, keeping their spacing within the clip.

    Clips recorded back to back - a camera splitting one recording at a file size
    limit - land exactly where their own timestamps would put them. Clips with a
    real gap between them have the gap removed, because the joined video has no
    gap either and the overlay has to match the picture rather than the clock.

    A segment with no points still advances the offset, so a clip whose GPS never
    locked leaves a hole rather than dragging everything after it earlier.
    """
    anchor: datetime | None = None
    for points, _ in segments:
        if points:
            anchor = min(p.timestamp for p in points)
            break
    if anchor is None:
        return []

    joined: list[DjiMetaPoint] = []
    offset_s = 0.0
    next_frame_idx = 0

    for points, duration_s in segments:
        if points:
            segment_start = min(p.timestamp for p in points)
            base = anchor + timedelta(seconds=offset_s)
            ordered = sorted(points, key=lambda p: p.timestamp)
            for index, point in enumerate(ordered):
                joined.append(
                    dc_replace(
                        point,
                        timestamp=base + (point.timestamp - segment_start),
                        frame_idx=next_frame_idx + index,
                    )
                )
            next_frame_idx += len(ordered)
        offset_s += duration_s

    return joined
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/services/test_joined_timeline.py -q`
Expected: 9 passed

- [ ] **Step 5: Lint and commit**

```bash
.venv/bin/python -m ruff check src tests && .venv/bin/python -m ruff format src tests
git add src/gpstitch/services/dji_meta_parser.py tests/unit/services/test_joined_timeline.py
git commit -m "Place several clips' GPS on one joined timeline"
```

---

### Task 4: Carry the merge request through the API

**Files:**
- Modify: `src/gpstitch/models/job.py:45-63`
- Modify: `src/gpstitch/api/render.py` (`BatchRenderRequest` ~line 473, `start_batch_render` ~line 565)
- Test: `tests/api/test_batch_merge.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `RenderJobConfig.merge_sources: list[str] | None`; `BatchRenderRequest.merge: bool`.

- [ ] **Step 1: Write the failing tests**

Create `tests/api/test_batch_merge.py`:

```python
"""Merging a batch produces one job, not one per file.

The point of the feature is a single rendered video whose overlay runs off one
continuous track. That only works if one job owns every clip.
"""

from pathlib import Path

import pytest

from gpstitch.services.job_manager import job_manager


def _videos(tmp_path, count=3):
    paths = []
    for i in range(count):
        p = tmp_path / f"DJI_004{i}_D.MP4"
        p.write_bytes(b"\0" * 16)
        paths.append(p)
    return paths


def _request(paths, **extra):
    return {
        "files": [{"video_path": str(p)} for p in paths],
        "layout": "default-1920x1080",
        **extra,
    }


class TestMergedBatch:
    async def test_merging_creates_a_single_job(self, async_client, tmp_path):
        paths = _videos(tmp_path, count=3)

        response = await async_client.post("/api/render/batch", json=_request(paths, merge=True))

        assert response.status_code == 200, response.text
        assert response.json()["total_jobs"] == 1

    async def test_the_job_carries_every_clip_in_order(self, async_client, tmp_path):
        paths = _videos(tmp_path, count=3)

        response = await async_client.post("/api/render/batch", json=_request(paths, merge=True))

        job = await job_manager.get_job(response.json()["job_ids"][0])
        assert job.config.merge_sources == [str(p) for p in paths]

    async def test_without_merging_each_clip_gets_its_own_job(self, async_client, tmp_path):
        paths = _videos(tmp_path, count=3)

        response = await async_client.post("/api/render/batch", json=_request(paths))

        assert response.json()["total_jobs"] == 3

    async def test_a_single_clip_is_not_merged(self, async_client, tmp_path):
        """One clip needs no join, so it takes the ordinary path."""
        paths = _videos(tmp_path, count=1)

        response = await async_client.post("/api/render/batch", json=_request(paths, merge=True))

        job = await job_manager.get_job(response.json()["job_ids"][0])
        assert job.config.merge_sources is None

    async def test_the_output_is_named_after_the_first_clip(self, async_client, tmp_path):
        paths = _videos(tmp_path, count=2)

        response = await async_client.post("/api/render/batch", json=_request(paths, merge=True))

        job = await job_manager.get_job(response.json()["job_ids"][0])
        assert Path(job.config.output_file).name.startswith("DJI_0040_D_merged_overlay")

    async def test_the_output_folder_is_honoured(self, async_client, tmp_path):
        paths = _videos(tmp_path, count=2)
        destination = tmp_path / "out"
        destination.mkdir()

        response = await async_client.post(
            "/api/render/batch", json=_request(paths, merge=True, output_dir=str(destination))
        )

        job = await job_manager.get_job(response.json()["job_ids"][0])
        assert Path(job.config.output_file).parent == destination
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/api/test_batch_merge.py -q`
Expected: FAIL — `total_jobs` is 3 for the merged request, and `merge_sources` does not exist

- [ ] **Step 3: Add the job config field**

In `src/gpstitch/models/job.py`, inside `RenderJobConfig` after `odo_offset`:

```python
    # Clips to join into one video before rendering, in play order. Set only for
    # a merged batch; the joined file becomes the session's primary during the
    # job's preparation.
    merge_sources: list[str] | None = None
```

- [ ] **Step 4: Add the request flag**

In `src/gpstitch/api/render.py`, inside `BatchRenderRequest` after `files`:

```python
    # Join the selected clips into one video and render it once, so the journey
    # map does not restart at every file boundary.
    merge: bool = False
```

- [ ] **Step 5: Branch the batch endpoint**

In `src/gpstitch/api/render.py`, immediately before the `for file_input in request.files:` loop in `start_batch_render`, insert:

```python
    # A merged batch is one job that owns every clip, so the whole ride shares a
    # single continuous track. One clip needs no join and takes the normal path.
    video_paths = [Path(f.video_path) for f in request.files if Path(f.video_path).exists()]
    if request.merge and len(video_paths) > 1:
        session_id = file_manager.create_local_session(skip_cleanup=True)
        first = video_paths[0]
        out_dir = Path(request.output_dir) if request.output_dir else first.parent
        output_file = str(out_dir / f"{first.stem}_merged_overlay{ext}")

        config = RenderJobConfig(
            session_id=session_id,
            layout=request.layout,
            layout_xml_path=request.layout_xml_path,
            output_file=output_file,
            units_speed=request.units_speed,
            units_altitude=request.units_altitude,
            units_distance=request.units_distance,
            units_temperature=request.units_temperature,
            map_style=request.map_style,
            gpx_merge_mode=request.gpx_merge_mode,
            # The joined file carries no embedded GPS, so it renders against the
            # combined GPX; this alignment mode makes render_service take the
            # video's start from that GPX.
            video_time_alignment="file-modified",
            time_offset_seconds=request.time_offset_seconds,
            ffmpeg_profile=request.ffmpeg_profile,
            gps_dop_max=request.gps_dop_max,
            gps_speed_max=request.gps_speed_max,
            merge_sources=[str(p) for p in video_paths],
        )
        job = await job_manager.create_job_with_batch(config, batch_id=batch_id)
        background_tasks.add_task(render_service.start_render, job.id, job.config)
        logger.info("Created merged batch %s from %d clips", batch_id, len(video_paths))
        return BatchRenderResponse(
            batch_id=batch_id,
            job_ids=[job.id],
            total_jobs=1,
            skipped_files=skipped_files,
        )
```

Note: `ext` is computed above this point in the function by
`get_output_extension_for_profile(request.ffmpeg_profile)`. If it is not yet in
scope at the insertion point, move that call above this block.

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/api/test_batch_merge.py -q`
Expected: 6 passed

- [ ] **Step 7: Run the whole backend suite**

Run: `.venv/bin/python -m pytest tests/unit tests/api -q`
Expected: all pass

- [ ] **Step 8: Lint and commit**

```bash
.venv/bin/python -m ruff check src tests && .venv/bin/python -m ruff format src tests
git add src/gpstitch/models/job.py src/gpstitch/api/render.py tests/api/test_batch_merge.py
git commit -m "Create one job for a merged batch"
```

---

### Task 5: Pre-check reports the single output

**Files:**
- Modify: `src/gpstitch/api/render.py` (`PreCheckRequest` ~line 132, `pre_check_batch_files` ~line 216)
- Test: `tests/api/test_precheck_merge.py`

**Interfaces:**
- Consumes: `BatchRenderRequest.merge` naming convention from Task 4.
- Produces: `PreCheckRequest.merge: bool`.

- [ ] **Step 1: Write the failing tests**

Create `tests/api/test_precheck_merge.py`:

```python
"""Pre-check has to describe the render that will actually happen.

With merging on there is one output, so warning about three files that would be
overwritten - none of which will be written - is worse than saying nothing.
"""

import pytest


def _videos(tmp_path, count=3):
    paths = []
    for i in range(count):
        p = tmp_path / f"DJI_004{i}_D.MP4"
        p.write_bytes(b"\0" * 16)
        paths.append(p)
    return paths


class TestMergedPreCheck:
    async def test_one_planned_output_for_a_merged_batch(self, async_client, tmp_path):
        paths = _videos(tmp_path, count=3)

        response = await async_client.post(
            "/api/render/pre-check",
            json={"files": [{"video_path": str(p)} for p in paths], "merge": True},
        )

        assert response.status_code == 200, response.text
        assert len(response.json()["planned_outputs"]) == 1

    async def test_the_planned_output_is_the_merged_name(self, async_client, tmp_path):
        paths = _videos(tmp_path, count=2)

        response = await async_client.post(
            "/api/render/pre-check",
            json={"files": [{"video_path": str(p)} for p in paths], "merge": True},
        )

        [output] = response.json()["planned_outputs"]
        assert "DJI_0040_D_merged_overlay" in output

    async def test_an_existing_merged_output_is_reported_as_a_conflict(self, async_client, tmp_path):
        paths = _videos(tmp_path, count=2)
        (tmp_path / "DJI_0040_D_merged_overlay.mp4").write_bytes(b"\0")

        response = await async_client.post(
            "/api/render/pre-check",
            json={"files": [{"video_path": str(p)} for p in paths], "merge": True},
        )

        assert len(response.json()["overwrite_conflicts"]) == 1

    async def test_without_merging_each_file_is_planned_separately(self, async_client, tmp_path):
        paths = _videos(tmp_path, count=3)

        response = await async_client.post(
            "/api/render/pre-check",
            json={"files": [{"video_path": str(p)} for p in paths]},
        )

        assert len(response.json()["planned_outputs"]) == 3

    async def test_gps_quality_is_still_reported_for_every_clip(self, async_client, tmp_path):
        """Merging does not make a clip's GPS less worth checking."""
        paths = _videos(tmp_path, count=3)

        response = await async_client.post(
            "/api/render/pre-check",
            json={"files": [{"video_path": str(p)} for p in paths], "merge": True},
        )

        assert len(response.json()["gps_files"]) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/api/test_precheck_merge.py -q`
Expected: FAIL — three planned outputs for the merged request

- [ ] **Step 3: Add the flag to the request model**

In `src/gpstitch/api/render.py`, inside `PreCheckRequest` after `files`:

```python
    # Mirrors BatchRenderRequest.merge, so the planned outputs describe the
    # render that will actually run.
    merge: bool = False
```

- [ ] **Step 4: Plan one output when merging**

In `pre_check_batch_files`, replace the two lines that plan an output per file:

```python
        output_path = output_dir / f"{video_path.stem}_overlay{ext}"
        planned_outputs.setdefault(str(output_path), []).append(str(video_path))
```

with:

```python
        # A merged batch writes one file, named after the first clip, however
        # many clips feed it.
        if request.merge and len(request.files) > 1:
            merged_stem = Path(request.files[0].video_path).stem
            output_path = output_dir / f"{merged_stem}_merged_overlay{ext}"
        else:
            output_path = output_dir / f"{video_path.stem}_overlay{ext}"
        planned_outputs.setdefault(str(output_path), []).append(str(video_path))
```

Then, so a merged batch does not report the same conflict once per clip, change
the overwrite check immediately below from appending unconditionally to:

```python
        if output_path.exists() and not any(c.output_path == str(output_path) for c in overwrite_conflicts):
            overwrite_conflicts.append(
                OverwriteConflict(
                    video_path=str(video_path),
                    output_path=str(output_path),
                )
            )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/api/test_precheck_merge.py -q`
Expected: 5 passed

- [ ] **Step 6: Lint and commit**

```bash
.venv/bin/python -m ruff check src tests && .venv/bin/python -m ruff format src tests
git add src/gpstitch/api/render.py tests/api/test_precheck_merge.py
git commit -m "Report the single planned output for a merged batch"
```

---

### Task 6: Do the merge during the job's preparation

**Files:**
- Create: `src/gpstitch/services/merge_preparation.py`
- Modify: `src/gpstitch/services/render_service.py` (preparation block ~line 340-400, cleanup `finally` ~line 596)
- Test: `tests/unit/services/test_merge_preparation.py`

**Interfaces:**
- Consumes: `check_mergeable`, `join_clips`, `MergeNotPossible` (Tasks 1-2); `points_on_joined_timeline` (Task 3); `RenderJobConfig.merge_sources` (Task 4).
- Produces: `prepare_merged_source(config: RenderJobConfig, scratch_dir: Path, on_progress: Callable[[str], None], on_process: Callable[[subprocess.Popen], None] | None = None) -> list[str]` returning the temp paths it created, in cleanup order.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/services/test_merge_preparation.py`:

```python
"""Turning a list of clips into one video plus one track, ready to render.

The joined file carries no embedded GPS - ffmpeg cannot remux the DJI telemetry
stream - so it has to render as video plus an external GPX. Registering it that
way is what makes the existing renderer handle it without changes.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gpstitch.models.job import RenderJobConfig


def _config(sources, session_id="s1"):
    return RenderJobConfig(
        session_id=session_id,
        layout="default-1920x1080",
        output_file="/tmp/out.mp4",
        video_time_alignment="file-modified",
        merge_sources=sources,
    )


@pytest.fixture
def clips(tmp_path):
    paths = []
    for i in range(2):
        p = tmp_path / f"clip{i}.mp4"
        p.write_bytes(b"\0" * 16)
        paths.append(p)
    return paths


@pytest.fixture
def stubs(monkeypatch, tmp_path):
    """Stand in for ffmpeg and the GPS parser."""
    from gpstitch.services import merge_preparation as module

    registered = []

    manager = MagicMock()
    manager.add_file.side_effect = lambda **kwargs: registered.append(kwargs)
    monkeypatch.setattr(module, "file_manager", manager)

    monkeypatch.setattr(module, "check_mergeable", lambda paths, scratch_dir: 32)
    monkeypatch.setattr(
        module, "join_clips",
        lambda paths, output, on_progress=None, on_process=None: (output.write_bytes(b"\0"), output)[1],
    )
    monkeypatch.setattr(module, "clip_duration_seconds", lambda path: 10.0)
    monkeypatch.setattr(module, "parse_dji_meta_file", lambda path: [])
    monkeypatch.setattr(module, "points_on_joined_timeline", lambda segments: [])
    monkeypatch.setattr(module, "dji_meta_to_gpx_file", lambda video, out, rate, points=None: (out.write_text("<gpx/>"), out)[1])
    return registered


class TestPrepareMergedSource:
    def test_the_joined_video_becomes_the_primary(self, clips, tmp_path, stubs):
        from gpstitch.services.merge_preparation import prepare_merged_source

        prepare_merged_source(_config([str(c) for c in clips]), tmp_path, on_progress=lambda m: None)

        primary = [r for r in stubs if r["role"].value == "primary"]
        assert len(primary) == 1
        assert primary[0]["file_type"] == "video"

    def test_the_combined_gpx_becomes_the_secondary(self, clips, tmp_path, stubs):
        """Without it the renderer has no track at all for the joined file."""
        from gpstitch.services.merge_preparation import prepare_merged_source

        prepare_merged_source(_config([str(c) for c in clips]), tmp_path, on_progress=lambda m: None)

        secondary = [r for r in stubs if r["role"].value == "secondary"]
        assert len(secondary) == 1
        assert secondary[0]["file_type"] == "gpx"

    def test_both_temp_files_are_returned_for_cleanup(self, clips, tmp_path, stubs):
        from gpstitch.services.merge_preparation import prepare_merged_source

        temps = prepare_merged_source(_config([str(c) for c in clips]), tmp_path, on_progress=lambda m: None)

        assert len(temps) == 2
        assert any(t.endswith(".mp4") for t in temps)
        assert any(t.endswith(".gpx") for t in temps)

    def test_progress_is_reported_for_each_stage(self, clips, tmp_path, stubs):
        """The join alone runs for a quarter of an hour."""
        from gpstitch.services.merge_preparation import prepare_merged_source

        messages = []
        prepare_merged_source(_config([str(c) for c in clips]), tmp_path, on_progress=messages.append)

        assert any("GPS" in m for m in messages), messages
        assert any("oin" in m for m in messages), messages

    def test_a_refused_merge_propagates(self, clips, tmp_path, stubs, monkeypatch):
        from gpstitch.services import merge_preparation as module
        from gpstitch.services.merge_preparation import prepare_merged_source
        from gpstitch.services.video_merge import MergeNotPossible

        def refuse(paths, scratch_dir):
            raise MergeNotPossible("Clips differ in resolution")

        monkeypatch.setattr(module, "check_mergeable", refuse)

        with pytest.raises(MergeNotPossible, match="resolution"):
            prepare_merged_source(_config([str(c) for c in clips]), tmp_path, on_progress=lambda m: None)

    def test_a_shared_gpx_skips_gps_extraction(self, clips, tmp_path, stubs, monkeypatch):
        """C4: a supplied track wins, so there is nothing to extract."""
        from gpstitch.services import merge_preparation as module
        from gpstitch.services.merge_preparation import prepare_merged_source

        called = []
        monkeypatch.setattr(module, "parse_dji_meta_file", lambda path: called.append(path) or [])

        shared = tmp_path / "ride.gpx"
        shared.write_text("<gpx/>")
        config = _config([str(c) for c in clips])
        config.shared_gpx_path = str(shared)

        prepare_merged_source(config, tmp_path, on_progress=lambda m: None)

        assert called == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/services/test_merge_preparation.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'gpstitch.services.merge_preparation'`

- [ ] **Step 3: Add the shared GPX field to the job config**

In `src/gpstitch/models/job.py`, inside `RenderJobConfig` after `merge_sources`:

```python
    # A track supplied for the whole batch. When set it replaces the clips' own
    # embedded GPS, so no extraction is needed.
    shared_gpx_path: str | None = None
```

In `src/gpstitch/api/render.py`, in the merged-batch block added in Task 4, pass
it through by adding `shared_gpx_path=request.shared_gpx_path,` to the
`RenderJobConfig(...)` call.

- [ ] **Step 4: Write the implementation**

Create `src/gpstitch/services/merge_preparation.py`:

```python
"""Turn a merged batch's clips into one video and one track, ready to render.

A DJI Osmo Action splits a ride into several files. Rendered separately each gets
its own journey map, so the joins show. Joining the clips first and rendering
once removes the boundary entirely.

ffmpeg cannot remux the DJI `djmd` telemetry stream, so the joined file carries
no embedded GPS. It renders as video plus an external GPX instead, which is why
the combined track is registered as the session's secondary file: that is the
shape `generate_cli_command` already knows how to build a command for.
"""

import logging
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path

from gopro_overlay.ffmpeg import FFMPEG

from gpstitch.constants import DEFAULT_GPS_TARGET_HZ
from gpstitch.models.job import RenderJobConfig
from gpstitch.models.schemas import FileRole
from gpstitch.services.dji_meta_parser import (
    derive_sample_rate,
    dji_meta_to_gpx_file,
    parse_dji_meta_file,
    points_on_joined_timeline,
)
from gpstitch.services.file_manager import file_manager
from gpstitch.services.video_merge import check_mergeable, join_clips

logger = logging.getLogger(__name__)


def clip_duration_seconds(path: Path) -> float:
    """The clip's duration, which sets where the next clip's GPS begins."""
    command = [
        FFMPEG().binary.replace("ffmpeg", "ffprobe"),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    return float(result.stdout.strip() or 0.0)


def prepare_merged_source(
    config: RenderJobConfig,
    scratch_dir: Path,
    on_progress: Callable[[str], None],
    on_process: Callable[[subprocess.Popen], None] | None = None,
) -> list[str]:
    """Join the clips and build their combined track, registering both.

    Returns:
        Paths of the temporary files created, for the caller to clean up.
    """
    paths = [Path(p) for p in (config.merge_sources or [])]
    on_progress(f"Preparing to merge {len(paths)} clips")
    check_mergeable(paths, scratch_dir)

    temp_files: list[str] = []
    token = uuid.uuid4().hex[:8]

    # The track first: if the clips' GPS cannot be read there is no point
    # spending a quarter of an hour copying them together.
    gpx_path = scratch_dir / f"gpstitch_merged_{token}.gpx"
    if config.shared_gpx_path:
        on_progress("Using the supplied GPX for the whole merged video")
        gpx_source = Path(config.shared_gpx_path)
    else:
        segments = []
        for index, path in enumerate(paths, start=1):
            on_progress(f"Reading GPS from {path.name} ({index} of {len(paths)})")
            segments.append((parse_dji_meta_file(path), clip_duration_seconds(path)))

        combined = points_on_joined_timeline(segments)
        if not combined:
            raise ValueError("None of the selected clips carry GPS data, so there is nothing to draw.")

        sample_rate = derive_sample_rate(combined, target_hz=DEFAULT_GPS_TARGET_HZ)
        dji_meta_to_gpx_file(paths[0], gpx_path, sample_rate, points=combined)
        temp_files.append(str(gpx_path))
        gpx_source = gpx_path
        on_progress(f"Combined {len(combined)} GPS points across {len(paths)} clips")

    joined_path = scratch_dir / f"gpstitch_merged_{token}.mp4"
    join_clips(paths, joined_path, on_progress=on_progress, on_process=on_process)
    temp_files.insert(0, str(joined_path))

    # Replace the session's files: the joined video plus its track is what the
    # renderer should see, not the clips it came from.
    file_manager.add_file(
        session_id=config.session_id,
        filename=joined_path.name,
        file_path=joined_path,
        file_type="video",
        role=FileRole.PRIMARY,
    )
    file_manager.add_file(
        session_id=config.session_id,
        filename=gpx_source.name,
        file_path=gpx_source,
        file_type="gpx",
        role=FileRole.SECONDARY,
    )

    logger.info("Merged %d clips into %s", len(paths), joined_path)
    return temp_files
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/services/test_merge_preparation.py -q`
Expected: 6 passed

- [ ] **Step 6: Call it from the job's preparation**

In `src/gpstitch/services/render_service.py`, in `start_render`, immediately
before the `command, srt_gpx_temp_files = await asyncio.to_thread(` call, insert:

```python
        merge_temp_files: list[str] = []
        if config.merge_sources:
            from gpstitch.services.merge_preparation import prepare_merged_source
            from gpstitch.services.video_merge import MergeNotPossible

            def register_join(process):
                self._join_process = process

            try:
                merge_temp_files = await asyncio.to_thread(
                    prepare_merged_source,
                    config,
                    Path(config.output_file).parent,
                    on_progress,
                    register_join,
                )
            except (MergeNotPossible, ValueError) as e:
                await job_manager.append_job_log(job_id, f"ERROR: {e}")
                await job_manager.update_job_status(job_id, JobStatus.FAILED, str(e))
                logger.error("Merge failed for job %s: %s", job_id, e)
                await _clear_current_job()
                return
            finally:
                self._join_process = None
```

In `RenderService.__init__`, add:

```python
        # The ffmpeg process joining a merged batch's clips, so a cancel can kill
        # a copy that would otherwise run for a quarter of an hour.
        self._join_process: subprocess.Popen | None = None
```

with `import subprocess` added to the module's imports.

In `cancel_render`, inside the `if not self._process:` branch and before the
`self._preparing_job_id == job_id` check, add:

```python
            # A merged batch may be in the middle of an 18-minute stream copy.
            if self._join_process is not None:
                with contextlib.suppress(Exception):
                    self._join_process.kill()
```

In the `finally` block at the end of `start_render`, add `merge_temp_files` to
the files that get cleaned up:

```python
            for temp_file in merge_temp_files:
                self._cleanup_temp_file(temp_file)
```

Make sure `merge_temp_files` is initialised to `[]` before the `try` that owns
that `finally`, so the name always exists.

- [ ] **Step 7: Run the whole backend suite**

Run: `.venv/bin/python -m pytest tests/unit tests/api -q`
Expected: all pass

- [ ] **Step 8: Lint and commit**

```bash
.venv/bin/python -m ruff check src tests && .venv/bin/python -m ruff format src tests
git add src/gpstitch/services/merge_preparation.py src/gpstitch/services/render_service.py src/gpstitch/models/job.py src/gpstitch/api/render.py tests/unit/services/test_merge_preparation.py
git commit -m "Join a merged batch's clips during the job's preparation"
```

---

### Task 7: The toggle in the batch modal

**Files:**
- Modify: `src/gpstitch/static/unified/js/components/BatchRenderModal.js` (markup ~line 98, payloads at ~line 617 and ~line 685)
- Test: `tests/e2e/test_batch_merge_toggle.py`

**Interfaces:**
- Consumes: `merge` on both `/api/render/pre-check` and `/api/render/batch` (Tasks 4-5).
- Produces: nothing for later tasks.

- [ ] **Step 1: Write the failing e2e tests**

Create `tests/e2e/test_batch_merge_toggle.py`:

```python
"""The merge toggle has to reach the server, in both requests.

Pre-check uses it to describe one output instead of several; the batch request
uses it to create one job instead of one per clip. A toggle that reaches only one
of them gives a warning about files that will never be written.
"""

import json

import pytest
from playwright.sync_api import Page, expect


def _open_batch_modal(page: Page):
    page.locator("#btn-batch-render").click()
    expect(page.locator("#batch-render-modal")).to_be_visible()


@pytest.mark.e2e
class TestMergeToggle:
    def test_the_toggle_is_offered(self, app_page: Page):
        _open_batch_modal(app_page)

        expect(app_page.locator("#batch-merge-toggle")).to_be_attached()

    def test_it_is_off_by_default(self, app_page: Page):
        """Merging rewrites what the batch produces; it should be asked for."""
        _open_batch_modal(app_page)

        expect(app_page.locator("#batch-merge-toggle")).not_to_be_checked()

    def test_the_flag_reaches_the_pre_check_request(self, app_page: Page):
        sent = []
        app_page.route(
            "**/api/render/pre-check",
            lambda route: (
                sent.append(json.loads(route.request.post_data)),
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({
                        "overwrite_conflicts": [], "duplicate_outputs": [],
                        "planned_outputs": {}, "gps_files": [], "gps_issues_count": 0,
                    }),
                ),
            )[-1],
        )

        _open_batch_modal(app_page)
        app_page.evaluate("""() => {
            window.app.batchRenderModal.videoFiles = ['/tmp/a.mp4', '/tmp/b.mp4'];
        }""")
        app_page.locator("#batch-merge-toggle").check()
        app_page.locator("#batch-start-btn").click()

        expect(app_page.locator("#batch-merge-toggle")).to_be_checked()
        assert sent and sent[0]["merge"] is True, sent
```

Note: the selectors `#batch-render-modal`, `#batch-start-btn` and the property
`window.app.batchRenderModal` must be confirmed against the current markup before
writing this test — read `BatchRenderModal.js`'s `_render()` and
`UnifiedApp.js`'s component wiring, and use the real ids. If the start button has
a different id, use that one.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/e2e/test_batch_merge_toggle.py -q`
Expected: FAIL — `#batch-merge-toggle` is not attached

- [ ] **Step 3: Add the toggle to the modal markup**

In `BatchRenderModal.js`'s `_render()`, next to the existing batch options, add:

```javascript
                        <label class="batch-option" title="Join the selected clips and render one video">
                            <input type="checkbox" id="batch-merge-toggle">
                            <span>Merge into one video</span>
                            <small class="config-hint">
                                Joins the clips end to end so the map runs across the whole ride.
                                Needs free disk equal to the clips' combined size while it renders.
                            </small>
                        </label>
```

- [ ] **Step 4: Send the flag in both payloads**

In the pre-check payload (~line 617), after `const payload = { files: files };`:

```javascript
        payload.merge = this._isMergeEnabled();
```

In the batch payload (~line 685), inside the `request` object literal:

```javascript
            merge: this._isMergeEnabled(),
```

And add the helper method to the class:

```javascript
    /** Whether the clips should be joined into one rendered video. */
    _isMergeEnabled() {
        return Boolean(document.getElementById('batch-merge-toggle')?.checked);
    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/e2e/test_batch_merge_toggle.py -q`
Expected: 3 passed

- [ ] **Step 6: Run the whole e2e suite**

Run: `.venv/bin/python -m pytest tests/e2e -q`
Expected: all pass (takes about 4 minutes)

- [ ] **Step 7: Lint and commit**

```bash
.venv/bin/python -m ruff check src tests && .venv/bin/python -m ruff format src tests
git add src/gpstitch/static/unified/js/components/BatchRenderModal.js tests/e2e/test_batch_merge_toggle.py
git commit -m "Offer merging in the batch render modal"
```

---

### Task 8: Document the feature

**Files:**
- Modify: `README.md` (Features list ~line 13, Batch Rendering section ~line 67)
- Modify: `CLAUDE.md` (the render-modes paragraph)

**Interfaces:**
- Consumes: the finished feature.
- Produces: nothing.

- [ ] **Step 1: Add the feature to the README list**

Under `## Features`, after the Batch Rendering bullet:

```markdown
- **Merged Batch Render** — Join a ride's clips into one video and render it once, so the journey map, odometer
  and place names run across the whole ride instead of restarting at every file boundary
```

- [ ] **Step 2: Describe it in the Batch Rendering section**

Append to that section:

```markdown
### Merging a split ride

Action cameras split a long recording into several files. Rendered separately,
each one gets its own journey map, so the joins are visible in the finished
video.

Tick **Merge into one video** in the batch dialog and the selected clips are
joined losslessly, their GPS is combined onto a single timeline, and one video is
rendered from the result. The joined source is removed afterwards — the output is
the single rendered file.

Every clip must share a resolution and codec, and the drive needs free space
equal to the clips' combined size while the render runs.
```

- [ ] **Step 3: Note the mode in CLAUDE.md**

In the "Architecture" section, after the four render modes, add:

```markdown
A merged batch is mode 2 in disguise: the clips are joined into one file during
the job's preparation (`services/merge_preparation.py`) and rendered against a
GPX combining every clip's GPS, because ffmpeg cannot carry the DJI telemetry
stream through a concat.
```

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "Document merged batch render"
```

---

## Self-Review

**Spec coverage:**

| Spec requirement | Task |
|---|---|
| Toggle on batch render | 7 |
| One job carrying all clips | 4 |
| Preflight: resolution/codec (C5), disk (C6) | 1 |
| Combined GPX on the joined timeline | 3, 6 |
| Lossless join (C7 process handed out) | 2 |
| Joined file as primary, GPX as secondary | 6 |
| `file-modified` alignment so mtime comes from the GPX | 4 (config), 6 |
| C1 selection-as-given | 4 (no grouping logic) |
| C2 joined file deleted | 6 (cleanup) |
| C3 single clip renders normally | 4 |
| C4 shared GPX wins | 6 |
| C8 output naming | 4 |
| C9 pre-check single output | 5 |
| C10, C11 no change needed | — |
| Documentation | 8 |

**Placeholder scan:** none — every step carries the code or the exact command.
Task 7 Step 1 carries a verification note rather than a guess, because the modal
ids must be read from the current markup.

**Type consistency:** `check_mergeable(paths, scratch_dir) -> int` and
`join_clips(paths, output, on_progress, on_process)` are used in Task 6 exactly
as defined in Tasks 1-2. `points_on_joined_timeline(segments)` takes
`list[tuple[list[DjiMetaPoint], float]]` in both Task 3 and Task 6.
`merge_sources` and `shared_gpx_path` are named identically in Tasks 4 and 6.
`merge` is the flag name on both request models and both front-end payloads.
