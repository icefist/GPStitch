"""Turn a merged batch's clips into one video and one track, ready to render.

A DJI Osmo Action splits a ride into several files. Rendered separately each gets
its own journey map, so the joins show in the finished video. Joining the clips
first and rendering once removes the boundary entirely.

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
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "csv=p=0",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    return float(result.stdout.strip() or 0.0)


# How much progress has to accumulate before it is worth another log line. The
# job log keeps its last 500 lines, and ffmpeg reports far more often than that
# allows - every update would push the useful lines out.
_PROGRESS_STEP_PERCENT = 10


def _percentage_reporter(
    label: str,
    duration_s: float,
    on_progress: Callable[[str], None],
) -> Callable[[float], None] | None:
    """Turn seconds-processed into occasional "label — NN%" lines.

    Returns None when the duration is unknown, which leaves the extraction on
    its quieter path rather than dividing by zero.
    """
    if duration_s <= 0:
        return None

    last_reported = -1

    def report(seconds: float) -> None:
        nonlocal last_reported
        # ffmpeg can overshoot slightly at the end of a stream.
        percent = min(100, int(seconds / duration_s * 100))
        step = percent - percent % _PROGRESS_STEP_PERCENT
        if step > last_reported:
            last_reported = step
            on_progress(f"{label} — {step}%")

    return report


def prepare_merged_source(
    config: RenderJobConfig,
    scratch_dir: Path,
    on_progress: Callable[[str], None],
    on_process: Callable[[subprocess.Popen], None] | None = None,
) -> list[str]:
    """Join the clips and build their combined track, registering both.

    Returns:
        Paths of the temporary files this created, for the caller to clean up.
        A GPX the user supplied is not among them - it is not ours to delete.
    """
    paths = [Path(p) for p in (config.merge_sources or [])]
    on_progress(f"Preparing to merge {len(paths)} clips")
    check_mergeable(paths, scratch_dir)

    temp_files: list[str] = []
    token = uuid.uuid4().hex[:8]

    # The track first: if the clips carry no GPS there is no point spending a
    # quarter of an hour copying them together.
    if config.shared_gpx_path:
        on_progress("Using the supplied GPX for the whole merged video")
        gpx_source = Path(config.shared_gpx_path)
    else:
        segments = []
        for index, path in enumerate(paths, start=1):
            duration = clip_duration_seconds(path)
            label = f"Reading GPS from {path.name} ({index} of {len(paths)})"
            on_progress(label)
            # Reading one clip takes minutes; report ffmpeg's position as a
            # percentage rather than leaving one line on screen throughout.
            report = _percentage_reporter(label, duration, on_progress)
            segments.append((parse_dji_meta_file(path, on_progress=report), duration))

        combined = points_on_joined_timeline(segments)
        if not combined:
            raise ValueError("None of the selected clips carry GPS data, so there would be nothing to draw.")

        gpx_source = scratch_dir / f"gpstitch_merged_{token}.gpx"
        dji_meta_to_gpx_file(
            paths[0],
            gpx_source,
            derive_sample_rate(combined, target_hz=DEFAULT_GPS_TARGET_HZ),
            points=combined,
        )
        temp_files.append(str(gpx_source))
        on_progress(f"Combined {len(combined)} GPS points across {len(paths)} clips")

    joined_path = scratch_dir / f"gpstitch_merged_{token}.mp4"
    join_clips(paths, joined_path, on_progress=on_progress, on_process=on_process)
    temp_files.insert(0, str(joined_path))

    # What the renderer should see is the joined video and its track, not the
    # clips they came from.
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
