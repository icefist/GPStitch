"""Metadata extraction service using gopro_overlay."""

import json
import logging
from pathlib import Path

from gpstitch.config import settings
from gpstitch.models.schemas import GpxFitMetadata, VideoMetadata

# Apply runtime patches if enabled
if settings.enable_gopro_patches:
    from gpstitch.patches import apply_patches

    apply_patches()

logger = logging.getLogger(__name__)


_VALID_ROTATIONS = {0, 90, 180, 270}


def get_video_rotation(file_path: Path) -> int:
    """Get video rotation magnitude from metadata.

    Returns one of 0, 90, 180, 270. Unrecognised values fall back to 0.
    """
    from gopro_overlay.ffmpeg import FFMPEG

    try:
        ffmpeg = FFMPEG()
        output = (
            ffmpeg.ffprobe().invoke(["-hide_banner", "-print_format", "json", "-show_streams", str(file_path)]).stdout
        )
        data = json.loads(str(output))
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                for sd in stream.get("side_data_list", []):
                    if "rotation" in sd:
                        rotation = abs(int(sd["rotation"]))
                        return rotation if rotation in _VALID_ROTATIONS else 0
                rotation_tag = stream.get("tags", {}).get("rotate")
                if rotation_tag:
                    rotation = abs(int(rotation_tag))
                    return rotation if rotation in _VALID_ROTATIONS else 0
    except Exception as e:
        logger.debug("Could not determine video rotation for %s: %s", file_path, e)
    return 0


def get_display_dimensions(width: int, height: int, rotation: int) -> tuple[int, int]:
    """Return (width, height) accounting for rotation."""
    if rotation in (90, 270):
        return height, width
    return width, height


def extract_video_metadata(file_path: Path) -> VideoMetadata | None:
    """Extract metadata from a video file using FFMPEGGoPro."""
    from gopro_overlay.ffmpeg import FFMPEG
    from gopro_overlay.ffmpeg_gopro import FFMPEGGoPro

    try:
        ffmpeg = FFMPEG()
        ffmpeg_gopro = FFMPEGGoPro(ffmpeg)
        recording = ffmpeg_gopro.find_recording(file_path)

        video = recording.video
        has_gps = recording.data is not None

        rotation = get_video_rotation(file_path)
        display_w, display_h = get_display_dimensions(video.dimension.x, video.dimension.y, rotation)

        # Detect an embedded DJI meta GPS stream.
        #
        # Detection only reads the container headers, so it costs about 0.15s
        # even on a multi-gigabyte file. Reading the stream's contents is a very
        # different proposition: its samples are interleaved across the whole
        # file, so counting the GPS points inside means seeking through all of
        # it - measured at ~390s for a 12GB clip on an external volume.
        #
        # That work is deferred to preview and render, which already extract the
        # stream themselves when GPS is actually needed, and which already raise
        # a clear error if the stream turns out to hold no usable points.
        has_dji_meta = False
        try:
            from gpstitch.services.dji_meta_parser import detect_dji_meta_stream

            has_dji_meta = detect_dji_meta_stream(file_path) is not None
        except Exception:
            logger.debug("DJI meta detection skipped for %s", file_path)

        return VideoMetadata(
            width=display_w,
            height=display_h,
            duration_seconds=video.duration.millis() / 1000.0,
            frame_count=video.frame_count,
            frame_rate=video.frame_rate(),
            has_gps=has_gps,
            has_dji_meta=has_dji_meta,
        )
    except Exception:
        logger.exception("Error extracting video metadata from %s", file_path)
        return None


def extract_gpx_fit_metadata(file_path: Path) -> GpxFitMetadata | None:
    """Extract metadata from a GPX, FIT, or SRT file."""
    try:
        if file_path.suffix.lower() == ".srt":
            from gpstitch.services.srt_parser import get_srt_metadata

            meta = get_srt_metadata(file_path)
            return GpxFitMetadata(
                gps_point_count=meta["gps_point_count"],
                duration_seconds=meta["duration_seconds"],
            )

        from gopro_overlay.loading import load_external
        from gopro_overlay.units import units

        timeseries = load_external(file_path, units)

        # Count GPS points
        point_count = len(timeseries)

        # Calculate duration if we have timestamps
        duration = None
        if point_count > 0:
            start_time = timeseries.min
            end_time = timeseries.max
            duration = (end_time - start_time).total_seconds()

        return GpxFitMetadata(
            gps_point_count=point_count,
            duration_seconds=duration,
        )
    except Exception:
        logger.exception("Error extracting GPX/FIT metadata from %s", file_path)
        return None


def get_file_type(file_path: Path) -> str:
    """Determine the file type from extension."""
    suffix = file_path.suffix.lower()
    return {".mp4": "video", ".mov": "video", ".gpx": "gpx", ".fit": "fit", ".srt": "srt"}.get(suffix, "unknown")
