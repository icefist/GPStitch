"""How long a clip is, and how to report progress through it.

Both the join and the GPS read walk a clip end to end and take minutes over it.
Neither can say how far along it is without knowing the clip's length, and both
would drown the job log if they reported every update ffmpeg offers - so the
duration probe and the throttle live together, one level below the two callers
that need them.
"""

import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

from gopro_overlay.ffmpeg import FFMPEG

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


def percentage_reporter(
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
