"""Refusing a join that would produce garbage, before anything is written.

Concatenating clips whose video streams differ decodes into rubbish after the
first boundary, and a join needs as much free space as the clips occupy. Both are
cheap to check and expensive to discover after an 18-minute copy.
"""

from unittest.mock import patch

import pytest

from gpstitch.services.video_merge import (
    ClipProfile,
    MergeNotPossible,
    check_mergeable,
)


def _clips(tmp_path, count=2, size=1024):
    paths = []
    for i in range(count):
        p = tmp_path / f"clip{i}.mp4"
        p.write_bytes(b"\0" * size)
        paths.append(p)
    return paths


class TestCheckMergeable:
    def test_matching_clips_return_their_total_size(self, tmp_path):
        paths = _clips(tmp_path, count=2, size=1024)
        profile = ClipProfile(width=2688, height=1512, video_codec="hevc", pix_fmt="yuv420p")

        with patch("gpstitch.services.video_merge.probe_clip", return_value=profile):
            total = check_mergeable(paths, scratch_dir=tmp_path)

        assert total == 2048

    def test_a_different_resolution_is_refused(self, tmp_path):
        paths = _clips(tmp_path)
        profiles = [
            ClipProfile(width=2688, height=1512, video_codec="hevc", pix_fmt="yuv420p"),
            ClipProfile(width=1920, height=1080, video_codec="hevc", pix_fmt="yuv420p"),
        ]

        with (
            patch("gpstitch.services.video_merge.probe_clip", side_effect=profiles),
            pytest.raises(MergeNotPossible, match="resolution"),
        ):
            check_mergeable(paths, scratch_dir=tmp_path)

    def test_a_different_codec_is_refused(self, tmp_path):
        paths = _clips(tmp_path)
        profiles = [
            ClipProfile(width=2688, height=1512, video_codec="hevc", pix_fmt="yuv420p"),
            ClipProfile(width=2688, height=1512, video_codec="h264", pix_fmt="yuv420p"),
        ]

        with (
            patch("gpstitch.services.video_merge.probe_clip", side_effect=profiles),
            pytest.raises(MergeNotPossible, match="codec"),
        ):
            check_mergeable(paths, scratch_dir=tmp_path)

    def test_a_different_pixel_format_is_refused(self, tmp_path):
        """A 10-bit clip joined onto 8-bit ones decodes as mush from the join on.

        The camera writes Main 10 for some recordings and Main for others, and
        both report codec `hevc` at the same resolution - so nothing shallower
        than the pixel format can tell them apart.
        """
        paths = _clips(tmp_path)
        profiles = [
            ClipProfile(width=2688, height=1512, video_codec="hevc", pix_fmt="yuv420p"),
            ClipProfile(width=2688, height=1512, video_codec="hevc", pix_fmt="yuv420p10le"),
        ]

        with (
            patch("gpstitch.services.video_merge.probe_clip", side_effect=profiles),
            pytest.raises(MergeNotPossible) as caught,
        ):
            check_mergeable(paths, scratch_dir=tmp_path)

        assert "clip1.mp4" in str(caught.value)
        assert "yuv420p10le" in str(caught.value)

    def test_the_refusal_names_both_clips(self, tmp_path):
        """ "They differ" is useless when the batch holds twenty files."""
        paths = _clips(tmp_path)
        profiles = [
            ClipProfile(width=2688, height=1512, video_codec="hevc", pix_fmt="yuv420p"),
            ClipProfile(width=1920, height=1080, video_codec="hevc", pix_fmt="yuv420p"),
        ]

        with (
            patch("gpstitch.services.video_merge.probe_clip", side_effect=profiles),
            pytest.raises(MergeNotPossible) as caught,
        ):
            check_mergeable(paths, scratch_dir=tmp_path)

        assert "clip1.mp4" in str(caught.value)
        assert "2688x1512" in str(caught.value)
        assert "1920x1080" in str(caught.value)

    def test_insufficient_disk_is_refused(self, tmp_path):
        paths = _clips(tmp_path, count=2, size=1024)
        profile = ClipProfile(width=2688, height=1512, video_codec="hevc", pix_fmt="yuv420p")

        class Usage:
            free = 512

        with (
            patch("gpstitch.services.video_merge.probe_clip", return_value=profile),
            patch("gpstitch.services.video_merge.shutil.disk_usage", return_value=Usage()),
            pytest.raises(MergeNotPossible, match="disk"),
        ):
            check_mergeable(paths, scratch_dir=tmp_path)

    def test_fewer_than_two_clips_is_refused(self, tmp_path):
        """A single clip needs no join; callers must not reach here with one."""
        with pytest.raises(MergeNotPossible, match="two"):
            check_mergeable(_clips(tmp_path, count=1), scratch_dir=tmp_path)


class TestConcatList:
    """ffmpeg's concat demuxer reads a list file with one `file '<path>'` per line."""

    def test_each_clip_gets_a_line(self, tmp_path):
        from gpstitch.services.video_merge import write_concat_list

        paths = _clips(tmp_path, count=2)
        listing = write_concat_list(paths, tmp_path / "list.txt")

        lines = listing.read_text(encoding="utf-8").strip().splitlines()
        assert lines == [f"file '{paths[0]}'", f"file '{paths[1]}'"]

    def test_a_quote_in_a_path_is_escaped(self, tmp_path):
        """An unescaped apostrophe ends the quoted string and ffmpeg misreads the path."""
        from gpstitch.services.video_merge import write_concat_list

        odd = tmp_path / "ride's clip.mp4"
        odd.write_bytes(b"\0")
        listing = write_concat_list([odd], tmp_path / "list.txt")

        assert listing.read_text(encoding="utf-8").strip() == f"file '{tmp_path}/ride'\\''s clip.mp4'"


class TestJoinClips:
    def test_the_ffmpeg_command_copies_streams(self, tmp_path):
        """A re-encode would take hours and change the picture."""
        from gpstitch.services.video_merge import join_clips

        captured = {}

        class FakeProcess:
            returncode = 0

            def communicate(self):
                return ("", "")

        def fake_popen(command, **kwargs):
            captured["command"] = command
            return FakeProcess()

        with patch("gpstitch.services.video_merge.subprocess.Popen", side_effect=fake_popen):
            join_clips(_clips(tmp_path), tmp_path / "out.mp4")

        command = captured["command"]
        assert "-c" in command and "copy" in command
        assert "concat" in command
        assert command[command.index("-map") + 1] == "0:v:0"

    def test_a_failed_join_raises_with_ffmpeg_stderr(self, tmp_path):
        from gpstitch.services.video_merge import join_clips

        class FakeProcess:
            returncode = 1

            def communicate(self):
                return ("", "Invalid data found when processing input")

        with (
            patch("gpstitch.services.video_merge.subprocess.Popen", return_value=FakeProcess()),
            pytest.raises(MergeNotPossible, match="Invalid data"),
        ):
            join_clips(_clips(tmp_path), tmp_path / "out.mp4")

    def test_the_process_is_handed_to_the_caller(self, tmp_path):
        """Cancelling must be able to kill an eighteen-minute copy, not wait it out."""
        from gpstitch.services.video_merge import join_clips

        class FakeProcess:
            returncode = 0

            def communicate(self):
                return ("", "")

        handed = []
        with patch("gpstitch.services.video_merge.subprocess.Popen", return_value=FakeProcess()):
            join_clips(_clips(tmp_path), tmp_path / "out.mp4", on_process=handed.append)

        assert len(handed) == 1
