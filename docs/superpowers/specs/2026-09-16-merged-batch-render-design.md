# Merged batch render

## Problem

A DJI Osmo Action splits a long ride into ~17.2GB files with incrementing
numbers. GPStitch renders each one separately, and `JourneyMap` builds its route
from the framemeta of the clip it is rendering, so the map restarts at every
file boundary. The joins are obvious in the finished video, and the clips have to
be stitched in DaVinci Resolve afterwards.

Measured on one card: auto-split parts are separated by **0 seconds** and a
consecutive file number, while a manual stop/start leaves minutes. A ride is
typically 2-3 files, e.g. `0041+0042+0043` = 1h41m over ~40GB.

## What this adds

A **Merge into one video** toggle on batch render. With it on, the selected
clips produce **one** rendered output whose overlay is driven by one continuous
GPS track: the journey map draws the whole ride, the odometer runs unbroken, and
place names span the ride.

## Approach

The merge happens inside a single render job's preparation phase - the phase that
already reports progress and honours cancellation. Rendering itself is unchanged.

Two alternatives were rejected:

- **Render each clip, then concatenate the outputs.** Does not solve the problem:
  each render still has its own framemeta, so each still gets its own map.
- **Stream the clips through ffmpeg's concat demuxer**, avoiding an intermediate
  file. `ffprobe -f concat` reports no duration, so `find_recording` and the
  render's ffmpeg invocation would both need patching to inject duration and
  dimensions. More moving parts for a disk saving the user does not need.

## Flow

With `merge` on, `POST /api/render/batch` creates **one** job carrying
`merge_sources` (the selected paths in filename order) rather than one job per
file. During that job's preparation:

1. **Preflight.** Every clip shares resolution and codec; free disk exceeds the
   summed size. Either failure stops the job before any work.
2. **GPS.** Read each clip's embedded GPS and write one GPX, each clip's points
   shifted onto the joined timeline by the cumulative duration of the clips
   before it.
3. **Join.** `ffmpeg -f concat -safe 0 -i list -map 0:v:0 -map 0:a:0 -c copy`.
   Lossless, no re-encode; measured at ~38MB/s reading from the camera card.
4. **Hand off.** Register the joined file as the session's primary video and the
   combined GPX as its secondary.

Then the existing command generation runs untouched.

### Why the joined file needs an external GPX

ffmpeg cannot remux the DJI `djmd` data stream, so the joined file carries no
embedded GPS and cannot use the DJI render mode. It renders as **video + GPX**
instead.

Time alignment then falls out of existing code. With
`video_time_alignment = "file-modified"` and a GPX secondary,
`render_service._resolve_mtime_for_alignment` already returns the GPX's start
timestamp and sets the video's mtime to it, and `generate_cli_command` already
emits `--video-time-start file-modified`. No new alignment code.

## Timeline mapping

The joined video's timeline is the sum of the clip durations, so GPS is placed by
**cumulative offset**, not by wall clock. For auto-split parts the two are
identical. For clips with a real gap between them the gap is compressed out: the
overlay stays correct against the video, while the clock widget drifts from real
time and the map draws a straight line across the jump. This is the accepted
consequence of merging whatever is selected.

## Constraints

Decisions taken during review, and the reasons they are not open questions:

- **C1. Grouping is "whatever is selected".** No auto-detection of rides. The
  0-second-gap rule exists and is reliable, but the user chose explicit control.
- **C2. The joined source file is deleted** once the render succeeds. It is an
  input, not a deliverable; the deliverable is the single rendered output.
- **C3. One selected file with merge on renders normally.** No join, no
  intermediate, no combined GPX.
- **C4. A shared GPX wins over embedded GPS.** If the batch supplies one, it
  becomes the track for the whole merged video and step 2 is skipped entirely.
- **C5. Mixed resolution or codec is refused** before anything is written.
  Concat would otherwise produce a file that decodes into garbage after the first
  boundary.
- **C6. Insufficient disk is refused** before anything is written, naming the
  space required and the space free.
- **C7. Cancellation kills the join.** The ffmpeg concat is a tracked subprocess,
  because waiting out an 18-minute join to honour a cancel is not acceptable.
- **C8. Output name is the first clip's stem + `_merged_overlay`** plus the
  profile's extension.
- **C9. Pre-check reports one planned output** when merge is on, not one per
  file, so collision detection matches what will actually be written.
- **C10. A clip whose GPS fix is stuck no longer loses its widgets.** Position
  widgets are dropped only when the *whole* track never moves; in a merged ride
  one dead clip is a frozen stretch within a track that does move. This is
  intended - the rest of the ride should still get its map.
- **C11. Preview stays per-clip.** The merged file exists only during the batch,
  so the preview map still shows a single clip.

## Files affected

| File | Change |
|---|---|
| `models/job.py` | `RenderJobConfig` += `merge_sources: list[str] \| None` |
| `api/render.py` | `BatchRenderRequest`/`PreCheckRequest` += `merge`; one job when merging; single planned output in pre-check |
| `services/render_service.py` | merge step in preparation; joined file in cleanup; join subprocess tracked for cancel |
| `services/video_merge.py` | **new** - preflight, concat list, the join, progress |
| `services/dji_meta_parser.py` | combined points across clips on the joined timeline |
| `static/unified/js/components/BatchRenderModal.js` | the toggle, in both payloads |

## Testing

- **Unit** - timeline offsetting; concat list construction; preflight for
  mismatched codecs, mismatched dimensions and insufficient disk.
- **API** - merge on creates one job carrying all sources, not N; pre-check
  reports one planned output.
- **Integration** - join two real fixture clips, assert the duration is the sum
  and both streams survive.
- **E2E** - the toggle reaches the request; the modal reports a single output.
