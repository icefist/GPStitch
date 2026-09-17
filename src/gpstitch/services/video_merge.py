"""Join several clips into one file, losslessly.

A DJI Osmo Action splits a long ride into ~17GB files. Rendered separately, each
gets its own journey map, so the joins show in the finished video. Joining the
clips first gives the renderer one continuous video and one continuous track.

The join is a stream copy - no re-encode - so it is bound by disk speed rather
than CPU, and it cannot change the picture.
"""

import json
import logging
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from gopro_overlay.ffmpeg import FFMPEG

from gpstitch.services.clip_progress import clip_duration_seconds, percentage_reporter

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
    pix_fmt: str

    def describe(self) -> str:
        return f"{self.width}x{self.height} {self.video_codec} {self.pix_fmt}"


def _ffprobe_binary() -> str:
    return FFMPEG().binary.replace("ffmpeg", "ffprobe")


def probe_clip(path: Path) -> ClipProfile:
    """Read the video stream's dimensions and codec."""
    command = [
        _ffprobe_binary(),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,codec_name,pix_fmt",
        "-of",
        "json",
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
        pix_fmt=str(stream["pix_fmt"]),
    )


def check_mergeable(paths: list[Path], scratch_dir: Path) -> int:
    """Refuse a join that cannot work, before anything is written.

    Returns:
        Total size in bytes of the source clips.

    Raises:
        MergeNotPossible: clips differ in resolution or codec, or the scratch
            directory has too little room.

    A differing pixel format is not refused. The camera records some clips
    10-bit and some 8-bit, and the transport stream the join writes carries each
    clip's codec configuration with it, so the mixture decodes correctly.
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
    scratch_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(scratch_dir).free
    if free < total + _HEADROOM_BYTES:
        raise MergeNotPossible(
            f"Not enough disk to join these clips: {_gb(total + _HEADROOM_BYTES)} needed, "
            f"{_gb(free)} free in {scratch_dir}"
        )

    return total


def _gb(size_bytes: float) -> str:
    return f"{size_bytes / 1_000_000_000:.1f}GB"


def join_clips(
    paths: list[Path],
    output: Path,
    on_progress: Callable[[str], None] | None = None,
    on_process: Callable[[subprocess.Popen], None] | None = None,
) -> Path:
    """Concatenate `paths` into `output`, without re-encoding.

    Each clip is remuxed to MPEG-TS in its own pass, and all of those are piped
    through one ffmpeg that writes the MP4. The indirection is the whole point.

    MP4 stores a track's codec configuration once, so joining clips by copy
    describes every one of them with the first clip's - and this camera records
    some clips 10-bit and some 8-bit, which report identically as `hevc` at the
    same resolution. The result decodes into coloured blocks from the boundary
    to the end of the video, with nothing said: ffmpeg exits 0 and does not warn
    even at `-loglevel warning`. A transport stream repeats the configuration
    before every keyframe, so each clip carries its own, and the `hev1` tag lets
    the MP4 keep them in the bitstream rather than demanding one for the track.

    ffmpeg's own concat demuxer cannot do this whatever the output container: it
    hands the packets over already described by the first clip. Hence one pass
    per clip. They are piped rather than staged on disk because a long ride's
    clips are tens of gigabytes and writing them twice needs room for both.

    Only video and audio are carried across: ffmpeg cannot remux the DJI `djmd`
    telemetry stream, so GPS comes from the original clips instead.

    `on_process` receives each running ffmpeg, so a cancelled job can kill the
    work under way rather than waiting out a quarter of an hour.
    """
    if on_progress:
        on_progress(f"Joining {len(paths)} clips into one video - copying, not re-encoding")
    logger.info("Joining %d clips into %s", len(paths), output)

    collector = subprocess.Popen(
        _collect_command(output, probe_clip(paths[0]).video_codec),
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if on_process:
        on_process(collector)

    try:
        offset_s = 0.0
        for index, path in enumerate(paths, start=1):
            duration = clip_duration_seconds(path)
            label = f"Joining {path.name} ({index} of {len(paths)})"
            if on_progress:
                on_progress(label)
            _feed_as_transport_stream(
                path,
                collector.stdin,
                offset_s,
                percentage_reporter(label, duration, on_progress) if on_progress else None,
                on_process,
            )
            offset_s += duration
    finally:
        # Until this closes, the collector waits for more clips that will never
        # come - including when a clip has just failed and we are on our way out.
        collector.stdin.close()
        problems = _drain(collector.stderr, report=None)
        collector.wait()

    if collector.returncode != 0:
        raise MergeNotPossible(f"Joining the clips failed: {' '.join(problems).strip()}")

    if on_progress:
        on_progress("Clips joined")
    return output


# The MP4 sample entry that lets a track keep its codec configuration in the
# bitstream. The everyday `hvc1` and `avc1` demand a single one for the whole
# track, which is precisely what clips of differing bit depth do not share.
_IN_BAND_TAGS = {"hevc": "hev1", "h264": "avc3"}


def _collect_command(output: Path, video_codec: str) -> list[str]:
    """The one ffmpeg that turns the piped transport stream into the MP4."""
    tag = _IN_BAND_TAGS.get(video_codec)
    return [
        FFMPEG().binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "mpegts",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        # An unknown codec gets ffmpeg's default tag: a wrong one is refused
        # outright, and the join would fail for every clip rather than only the
        # mismatched ones.
        *(["-tag:v", tag] if tag else []),
        str(output),
    ]


# ffmpeg reports its position under either key, both in microseconds.
_PROGRESS_KEYS = ("out_time_us=", "out_time_ms=")


def _feed_as_transport_stream(
    path: Path,
    destination,
    offset_s: float,
    report: Callable[[float], None] | None,
    on_process: Callable[[subprocess.Popen], None] | None,
) -> None:
    """Stream-copy one clip into the collector's pipe as MPEG-TS.

    The offset matters as much as the copy. Without it every clip's timestamps
    restart at zero, and the joined file reports the length of a single clip -
    which is what the renderer sizes its work from.
    """
    command = [
        FFMPEG().binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-progress",
        "pipe:2",
        "-nostats",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-output_ts_offset",
        f"{offset_s:.6f}",
        # Left at their defaults the muxer shifts every clip by its own small
        # preload, which would drift the joins apart over a long ride.
        "-muxdelay",
        "0",
        "-muxpreload",
        "0",
        "-f",
        "mpegts",
        "pipe:1",
    ]

    process = subprocess.Popen(command, stdout=destination, stderr=subprocess.PIPE, text=True)
    if on_process:
        on_process(process)

    problems = _drain(process.stderr, report)
    if process.wait() != 0:
        raise MergeNotPossible(f"Joining {path.name} failed: {' '.join(problems).strip()}")


def _drain(stderr, report: Callable[[float], None] | None) -> list[str]:
    """Read ffmpeg's progress as it goes, keeping anything that is not progress.

    `-progress` writes `key=value` lines down the same pipe as the diagnostics,
    so whatever does not parse as progress is what went wrong.
    """
    problems: list[str] = []
    for raw in stderr:
        line = raw.strip()
        if not line:
            continue
        if line.startswith(_PROGRESS_KEYS):
            if report:
                report(int(line.split("=", 1)[1]) / 1_000_000)
        elif "=" not in line:
            problems.append(line)
    return problems
