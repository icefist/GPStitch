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
        profile = ClipProfile(width=2688, height=1512, video_codec="hevc")

        with patch("gpstitch.services.video_merge.probe_clip", return_value=profile):
            total = check_mergeable(paths, scratch_dir=tmp_path)

        assert total == 2048

    def test_a_different_resolution_is_refused(self, tmp_path):
        paths = _clips(tmp_path)
        profiles = [
            ClipProfile(width=2688, height=1512, video_codec="hevc"),
            ClipProfile(width=1920, height=1080, video_codec="hevc"),
        ]

        with (
            patch("gpstitch.services.video_merge.probe_clip", side_effect=profiles),
            pytest.raises(MergeNotPossible, match="resolution"),
        ):
            check_mergeable(paths, scratch_dir=tmp_path)

    def test_a_different_codec_is_refused(self, tmp_path):
        paths = _clips(tmp_path)
        profiles = [
            ClipProfile(width=2688, height=1512, video_codec="hevc"),
            ClipProfile(width=2688, height=1512, video_codec="h264"),
        ]

        with (
            patch("gpstitch.services.video_merge.probe_clip", side_effect=profiles),
            pytest.raises(MergeNotPossible, match="codec"),
        ):
            check_mergeable(paths, scratch_dir=tmp_path)

    def test_the_refusal_names_both_clips(self, tmp_path):
        """ "They differ" is useless when the batch holds twenty files."""
        paths = _clips(tmp_path)
        profiles = [
            ClipProfile(width=2688, height=1512, video_codec="hevc"),
            ClipProfile(width=1920, height=1080, video_codec="hevc"),
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
        profile = ClipProfile(width=2688, height=1512, video_codec="hevc")

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
