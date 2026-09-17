"""Refusing a join that would produce garbage, before anything is written.

Concatenating clips whose video streams differ decodes into rubbish after the
first boundary, and a join needs as much free space as the clips occupy. Both are
cheap to check and expensive to discover after an 18-minute copy.
"""

from unittest.mock import MagicMock, patch

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

    def test_clips_that_differ_only_in_pixel_format_are_allowed(self, tmp_path):
        """The camera records some clips 10-bit and some 8-bit.

        MP4 could not carry both - it stores the codec configuration once per
        track - so this used to decode as coloured blocks from the join to the
        end of the video. A transport stream carries the configuration with
        every clip, so the mixture is fine and must not be refused.
        """
        paths = _clips(tmp_path)
        profiles = [
            ClipProfile(width=2688, height=1512, video_codec="hevc", pix_fmt="yuv420p"),
            ClipProfile(width=2688, height=1512, video_codec="hevc", pix_fmt="yuv420p10le"),
        ]

        with patch("gpstitch.services.video_merge.probe_clip", side_effect=profiles):
            assert check_mergeable(paths, scratch_dir=tmp_path) == 2048

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


class TestJoinClips:
    """Each clip is remuxed to MPEG-TS in its own pass, piped into one MP4.

    The concat demuxer cannot do this: it hands every clip's packets over with
    the first clip's codec configuration, whatever the output container.
    """

    def _run(self, tmp_path, count=3, durations=10.0, returncode=0, producer_stderr=(), codec="hevc", **kwargs):
        from gpstitch.services import video_merge

        commands = []
        processes = []

        class FakeProcess:
            def __init__(self, is_collector):
                self.returncode = 0 if is_collector else returncode
                self.stderr = iter(() if is_collector else producer_stderr)
                self.stdin = MagicMock() if is_collector else None

            def wait(self):
                return self.returncode

        def fake_popen(command, **popen_kwargs):
            commands.append(command)
            process = FakeProcess(is_collector=not processes)
            processes.append(process)
            return process

        with (
            patch.object(video_merge.subprocess, "Popen", side_effect=fake_popen),
            patch.object(video_merge, "clip_duration_seconds", return_value=durations),
            patch.object(
                video_merge,
                "probe_clip",
                return_value=ClipProfile(width=2688, height=1512, video_codec=codec, pix_fmt="yuv420p"),
            ),
        ):
            video_merge.join_clips(_clips(tmp_path, count=count), tmp_path / "out.mp4", **kwargs)

        return commands[0], commands[1:], processes

    def test_each_clip_is_remuxed_in_its_own_pass(self, tmp_path):
        _, producers, _ = self._run(tmp_path, count=3)

        assert len(producers) == 3

    def test_each_clip_is_handed_over_as_a_transport_stream(self, tmp_path):
        """Only a transport stream repeats the codec configuration per clip."""
        _, producers, _ = self._run(tmp_path, count=2)

        for command in producers:
            assert command[command.index("-f") + 1] == "mpegts"
            assert command[command.index("-c") + 1] == "copy"

    def test_the_collector_writes_an_mp4_that_allows_per_clip_configuration(self, tmp_path):
        """`hvc1` would demand one configuration for the whole track."""
        collector, _, _ = self._run(tmp_path, count=2)

        assert collector[collector.index("-tag:v") + 1] == "hev1"
        assert collector[collector.index("-c") + 1] == "copy"
        assert str(tmp_path / "out.mp4") in collector

    def test_an_h264_join_gets_the_h264_tag(self, tmp_path):
        """`hev1` is HEVC's. Handing it to an H.264 track is refused outright,
        and the collector dies before the first clip reaches it."""
        collector, _, _ = self._run(tmp_path, count=2, codec="h264")

        assert collector[collector.index("-tag:v") + 1] == "avc3"

    def test_an_unfamiliar_codec_gets_no_tag_at_all(self, tmp_path):
        """A wrong tag fails every join; ffmpeg's own default fails none."""
        collector, _, _ = self._run(tmp_path, count=2, codec="vp9")

        assert "-tag:v" not in collector

    def test_nothing_is_re_encoded(self, tmp_path):
        """A re-encode would take hours and change the picture."""
        collector, producers, _ = self._run(tmp_path, count=2)

        for command in [collector, *producers]:
            assert not any(arg.startswith("-c:v") or arg in ("-vcodec",) for arg in command)

    def test_each_clip_starts_where_the_one_before_it_ended(self, tmp_path):
        """Without the offset every clip restarts at zero, and the joined file
        reports the duration of a single clip - which is what sizes the render."""
        _, producers, _ = self._run(tmp_path, count=3, durations=10.0)

        offsets = [float(c[c.index("-output_ts_offset") + 1]) for c in producers]
        assert offsets == [0.0, 10.0, 20.0]

    def test_every_ffmpeg_is_handed_to_the_caller(self, tmp_path):
        """Cancelling has to kill whatever is running, not just the first thing."""
        handed = []

        _, _, processes = self._run(tmp_path, count=3, on_process=handed.append)

        assert handed == processes

    def test_the_collector_is_told_when_the_clips_run_out(self, tmp_path):
        """Left open it waits for clips that will never come, and never finishes."""
        _, _, processes = self._run(tmp_path, count=2)

        processes[0].stdin.close.assert_called_once()

    def test_progress_names_the_clip_and_its_place_in_the_run(self, tmp_path):
        lines = []

        self._run(tmp_path, count=2, on_progress=lines.append)

        assert any("clip1.mp4" in line and "2 of 2" in line for line in lines)

    def test_a_failed_clip_raises_with_what_ffmpeg_said(self, tmp_path):
        from gpstitch.services.video_merge import MergeNotPossible

        with pytest.raises(MergeNotPossible, match="Invalid data"):
            self._run(tmp_path, count=2, returncode=1, producer_stderr=["Invalid data found\n"])

    def test_a_failed_clip_still_closes_the_collector(self, tmp_path):
        """Otherwise the failure leaves an ffmpeg waiting on a pipe forever."""
        from gpstitch.services.video_merge import MergeNotPossible

        collected = []

        def remember(process):
            collected.append(process)

        with pytest.raises(MergeNotPossible):
            self._run(tmp_path, count=2, returncode=1, producer_stderr=["boom\n"], on_process=remember)

        collected[0].stdin.close.assert_called_once()
