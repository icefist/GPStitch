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
        MergeNotPossible: clips differ in resolution, codec or pixel format, or
            the scratch directory has too little room.

    The pixel format matters as much as the codec. A camera that records one
    clip 10-bit and the next 8-bit reports `hevc` at the same resolution for
    both, but MP4 stores the codec configuration once per track - so a copied
    join decodes every frame of the odd clip with the wrong one, and the picture
    turns to coloured blocks from the join to the end of the video.
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
        if profile.pix_fmt != first_profile.pix_fmt:
            raise MergeNotPossible(
                f"Clips differ in pixel format: {paths[0].name} is {first_profile.describe()}, "
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
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(listing),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
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
