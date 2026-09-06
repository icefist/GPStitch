"""Windowed DJI GPS extraction.

Rendering one preview frame does not need the whole GPS stream. Extracting all
of it means seeking across the entire file - measured at ~390s for a 12GB clip -
while seeking to a frame's time and taking a few seconds around it costs ~0.5s
and yields ~200 points. A coarse sample spread across the clip (~5s) keeps the
journey map and odometer looking right without paying for the full stream.
"""

from unittest.mock import MagicMock, patch

import pytest

from gpstitch.services.dji_meta_parser import (
    extract_dji_meta_raw,
    sample_dji_meta_track,
)


@pytest.fixture
def captured_commands():
    """Capture the ffmpeg command lines instead of running them."""
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stdout = b""
        result.stderr = b""
        return result

    with (
        patch("gpstitch.services.dji_meta_parser.subprocess.run", side_effect=fake_run),
        patch("gpstitch.services.dji_meta_parser.FFMPEG") as ffmpeg,
    ):
        ffmpeg.return_value.binary = "ffmpeg"
        yield commands


class TestWindowedExtraction:
    def test_start_seeks_before_the_input(self, captured_commands, tmp_path):
        """-ss before -i is the fast form; after -i it decodes from the start."""
        extract_dji_meta_raw(tmp_path / "v.mp4", 2, start_s=900.0)
        cmd = captured_commands[0]
        assert "-ss" in cmd
        assert cmd.index("-ss") < cmd.index("-i")

    def test_duration_is_passed_as_an_output_limit(self, captured_commands, tmp_path):
        extract_dji_meta_raw(tmp_path / "v.mp4", 2, duration_s=6.0)
        cmd = captured_commands[0]
        assert "-t" in cmd
        assert cmd[cmd.index("-t") + 1] == "6.0"

    def test_full_extraction_is_unchanged(self, captured_commands, tmp_path):
        """Render still needs the whole stream; that path must not gain limits."""
        extract_dji_meta_raw(tmp_path / "v.mp4", 2)
        cmd = captured_commands[0]
        assert "-ss" not in cmd
        assert "-t" not in cmd

    def test_the_stream_is_still_selected_and_copied(self, captured_commands, tmp_path):
        extract_dji_meta_raw(tmp_path / "v.mp4", 2, start_s=10.0, duration_s=6.0)
        cmd = captured_commands[0]
        assert "-map" in cmd and cmd[cmd.index("-map") + 1] == "0:2"
        assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy"


class TestCoarseTrackSample:
    def test_takes_the_requested_number_of_windows(self, captured_commands, tmp_path):
        sample_dji_meta_track(tmp_path / "v.mp4", total_duration_s=2000.0, samples=20, stream_index=2)
        assert len(captured_commands) == 20

    def test_windows_are_spread_across_the_whole_clip(self, captured_commands, tmp_path):
        sample_dji_meta_track(tmp_path / "v.mp4", total_duration_s=2000.0, samples=5, stream_index=2)
        starts = [float(c[c.index("-ss") + 1]) for c in captured_commands]
        assert starts[0] == 0.0
        assert starts == sorted(starts)
        assert starts[-1] < 2000.0
        # Evenly spaced, not clustered at the start.
        assert starts[-1] > 1000.0

    def test_a_zero_length_clip_takes_one_sample(self, captured_commands, tmp_path):
        sample_dji_meta_track(tmp_path / "v.mp4", total_duration_s=0.0, samples=20, stream_index=2)
        assert len(captured_commands) == 1

    def test_a_failing_window_does_not_abort_the_sample(self, tmp_path):
        """One bad seek must not lose the whole track."""
        calls = {"n": 0}

        def flaky(cmd, **kwargs):
            calls["n"] += 1
            result = MagicMock()
            result.returncode = 1 if calls["n"] == 2 else 0
            result.stdout = b""
            result.stderr = b"seek failed"
            return result

        with (
            patch("gpstitch.services.dji_meta_parser.subprocess.run", side_effect=flaky),
            patch("gpstitch.services.dji_meta_parser.FFMPEG") as ffmpeg,
        ):
            ffmpeg.return_value.binary = "ffmpeg"
            points = sample_dji_meta_track(tmp_path / "v.mp4", total_duration_s=100.0, samples=4, stream_index=2)

        assert points == []
        assert calls["n"] == 4


class TestFirstPoint:
    """Finding the first GPS point must not read the whole stream.

    The start date comes from points[0] alone, but was obtained by parsing the
    entire stream - a full seek through the file to read its first record.
    """

    def test_reads_from_the_start_not_the_whole_stream(self, captured_commands, tmp_path):
        from gpstitch.services.dji_meta_parser import first_dji_meta_point

        first_dji_meta_point(tmp_path / "v.mp4", stream_index=2)
        assert captured_commands, "no extraction attempted"
        first = captured_commands[0]
        assert float(first[first.index("-ss") + 1]) == 0.0
        assert "-t" in first, "must bound the read"

    def test_widens_the_window_when_no_fix_yet(self, tmp_path):
        """A camera may take a minute to acquire GPS; the first window can be empty."""
        widths = []

        def fake_run(cmd, **kwargs):
            widths.append(float(cmd[cmd.index("-t") + 1]))
            result = MagicMock()
            result.returncode = 0
            result.stdout = b""
            result.stderr = b""
            return result

        with (
            patch("gpstitch.services.dji_meta_parser.subprocess.run", side_effect=fake_run),
            patch("gpstitch.services.dji_meta_parser.FFMPEG") as ffmpeg,
        ):
            ffmpeg.return_value.binary = "ffmpeg"
            got = first_dji_meta_point_ref()(tmp_path / "v.mp4", stream_index=2)

        assert got is None
        assert len(widths) > 1, "should retry with a wider window"
        assert widths == sorted(widths), "windows should widen, not shrink"

    def test_stops_at_the_first_window_that_has_points(self, tmp_path):
        from gpstitch.services.dji_meta_parser import first_dji_meta_point

        calls = {"n": 0}
        point = MagicMock()

        def fake_run(cmd, **kwargs):
            calls["n"] += 1
            result = MagicMock()
            result.returncode = 0
            result.stdout = b"x"
            result.stderr = b""
            return result

        with (
            patch("gpstitch.services.dji_meta_parser.subprocess.run", side_effect=fake_run),
            patch("gpstitch.services.dji_meta_parser.FFMPEG") as ffmpeg,
            patch("gpstitch.services.dji_meta_parser.parse_dji_meta", return_value=[point]),
        ):
            ffmpeg.return_value.binary = "ffmpeg"
            got = first_dji_meta_point(tmp_path / "v.mp4", stream_index=2)

        assert got is point
        assert calls["n"] == 1, "should not keep widening once points are found"


def first_dji_meta_point_ref():
    from gpstitch.services.dji_meta_parser import first_dji_meta_point

    return first_dji_meta_point
