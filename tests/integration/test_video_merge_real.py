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
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "csv=p=0",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() != ""


@pytest.mark.integration
def test_the_concat_list_is_cleaned_up(tmp_path):
    """It sits next to the output; leaving it behind litters the render folder."""
    output = tmp_path / "joined.mp4"

    join_clips([FIXTURE, FIXTURE], output)

    assert list(tmp_path.glob("*_concat.txt")) == []
