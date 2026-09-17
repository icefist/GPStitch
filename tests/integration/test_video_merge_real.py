"""Joining real clips, to prove the stream copy actually works.

The unit tests assert the command; this asserts ffmpeg accepts it, that the
result is the sum of its parts, and - the reason the join works the way it does
- that clips recorded at different bit depths decode cleanly across the join.
"""

import subprocess
from pathlib import Path

import pytest

from gpstitch.services.video_merge import join_clips

VIDEOS = Path(__file__).parent.parent / "fixtures" / "videos"
FIXTURE = VIDEOS / "DJI_20260315180109_0003_D_5s_fixture.MP4"

# Half a second each from a ride where the camera recorded one clip Main and the
# next Main 10. Joined as MP4 they decode as coloured blocks from the boundary
# on; they are here so that can never come back.
EIGHT_BIT = VIDEOS / "dji_hevc_8bit_fixture.MP4"
TEN_BIT = VIDEOS / "dji_hevc_10bit_fixture.MP4"


def _duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def _decode_errors(path: Path) -> list[str]:
    """Everything the decoder complains about while reading the file end to end."""
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [line for line in result.stderr.splitlines() if line.strip()]


def _frame_count(path: Path) -> int:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip().splitlines()[0])


@pytest.mark.integration
def test_joining_a_clip_to_itself_doubles_its_duration(tmp_path):
    """The same file twice is a valid join and the arithmetic is unambiguous."""
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
class TestMixedBitDepths:
    """The bug this join exists to avoid.

    MP4 stores a track's codec configuration once, so joining an 8-bit and a
    10-bit clip by copy described both with the first one's - and 44 minutes of
    a ride decoded into coloured blocks.
    """

    def test_the_clips_really_do_differ(self):
        """If the fixtures ever stop differing, the test below proves nothing."""
        assert _pix_fmt(EIGHT_BIT) == "yuv420p"
        assert _pix_fmt(TEN_BIT) == "yuv420p10le"

    def test_the_join_decodes_without_a_single_error(self, tmp_path):
        output = tmp_path / "mixed.mp4"

        join_clips([EIGHT_BIT, TEN_BIT], output)

        assert _decode_errors(output) == []

    def test_every_frame_of_both_clips_is_there(self, tmp_path):
        output = tmp_path / "mixed.mp4"

        join_clips([EIGHT_BIT, TEN_BIT], output)

        assert _frame_count(output) == _frame_count(EIGHT_BIT) + _frame_count(TEN_BIT)

    def test_the_joined_file_is_as_long_as_both_clips(self, tmp_path):
        """Without a timestamp offset per clip it reports the length of one."""
        output = tmp_path / "mixed.mp4"

        join_clips([EIGHT_BIT, TEN_BIT], output)

        assert _duration(output) == pytest.approx(_duration(EIGHT_BIT) + _duration(TEN_BIT), rel=0.05)


@pytest.mark.integration
def test_the_renderer_can_probe_the_joined_file(tmp_path):
    """gopro-overlay probes the video on every render, merged or not.

    It insists on a video stream marked default and reads `nb_frames` off it.
    An MPEG-TS - the obvious way to give each clip its own codec configuration -
    provides neither, so joining into one would fail every merged render.
    """
    from gopro_overlay.ffmpeg import FFMPEG
    from gopro_overlay.ffmpeg_gopro import FFMPEGGoPro

    output = tmp_path / "joined.mp4"

    join_clips([EIGHT_BIT, TEN_BIT], output)

    recording = FFMPEGGoPro(FFMPEG()).find_recording(output)
    assert recording.video.frame_count == _frame_count(EIGHT_BIT) + _frame_count(TEN_BIT)


def _pix_fmt(path: Path) -> str:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=pix_fmt",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()
