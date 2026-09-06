"""Loading a video must not extract the whole DJI GPS stream.

Extracting it requires seeking across the entire file. Measured on a 12GB clip
on an external volume: ~390s, versus 0.15s for the ffprobe that detects the
stream. Preview and render already extract it themselves when GPS is actually
needed, and both already raise a clear error when the stream holds no points -
so doing it at load time was duplicated work whose only output was a field
nothing read.
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def fake_recording():
    """Enough of an ffmpeg recording for get_video_metadata to work."""
    video = MagicMock()
    video.dimension.x = 1920
    video.dimension.y = 1080
    video.duration.millis.return_value = 1_951_000
    video.frame_count = 48_775
    video.frame_rate.return_value = 25.0

    recording = MagicMock()
    recording.video = video
    recording.gpmd = None
    return recording


def _get_metadata(path):
    from gpstitch.services.metadata import extract_video_metadata

    return extract_video_metadata(path)


class TestLoadDoesNotExtractTheStream:
    def test_full_extraction_is_not_called_at_load(self, tmp_path, fake_recording):
        """The expensive path must not run just to load a file."""
        video = tmp_path / "DJI_0021.MP4"
        video.write_bytes(b"fake")

        with (
            patch("gopro_overlay.ffmpeg_gopro.FFMPEGGoPro") as gopro,
            patch("gpstitch.services.dji_meta_parser.detect_dji_meta_stream", return_value=2),
            patch("gpstitch.services.dji_meta_parser.get_dji_meta_metadata") as expensive,
        ):
            gopro.return_value.find_recording.return_value = fake_recording
            _get_metadata(video)

        expensive.assert_not_called()

    def test_detected_stream_means_the_video_reports_gps(self, tmp_path, fake_recording):
        video = tmp_path / "DJI_0021.MP4"
        video.write_bytes(b"fake")

        with (
            patch("gopro_overlay.ffmpeg_gopro.FFMPEGGoPro") as gopro,
            patch("gpstitch.services.dji_meta_parser.detect_dji_meta_stream", return_value=2),
        ):
            gopro.return_value.find_recording.return_value = fake_recording
            meta = _get_metadata(video)

        assert meta.has_dji_meta is True

    def test_no_stream_means_no_dji_gps(self, tmp_path, fake_recording):
        video = tmp_path / "plain.MP4"
        video.write_bytes(b"fake")

        with (
            patch("gopro_overlay.ffmpeg_gopro.FFMPEGGoPro") as gopro,
            patch("gpstitch.services.dji_meta_parser.detect_dji_meta_stream", return_value=None),
        ):
            gopro.return_value.find_recording.return_value = fake_recording
            meta = _get_metadata(video)

        assert meta.has_dji_meta is False

    def test_a_failing_probe_does_not_break_the_load(self, tmp_path, fake_recording):
        """Detection is best-effort; a broken probe must not fail the whole load."""
        video = tmp_path / "DJI_0021.MP4"
        video.write_bytes(b"fake")

        with (
            patch("gopro_overlay.ffmpeg_gopro.FFMPEGGoPro") as gopro,
            patch(
                "gpstitch.services.dji_meta_parser.detect_dji_meta_stream",
                side_effect=RuntimeError("ffprobe exploded"),
            ),
        ):
            gopro.return_value.find_recording.return_value = fake_recording
            meta = _get_metadata(video)

        assert meta is not None
        assert meta.has_dji_meta is False
