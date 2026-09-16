"""Turning a list of clips into one video plus one track, ready to render.

The joined file carries no embedded GPS - ffmpeg cannot remux the DJI telemetry
stream - so it has to render as video plus an external GPX. Registering it that
way is what lets the existing renderer handle it without changes.
"""

from unittest.mock import MagicMock

import pytest

from gpstitch.models.job import RenderJobConfig


def _config(sources, session_id="s1", shared_gpx_path=None):
    return RenderJobConfig(
        session_id=session_id,
        layout="default-1920x1080",
        output_file="/tmp/out.mp4",
        video_time_alignment="file-modified",
        merge_sources=sources,
        shared_gpx_path=shared_gpx_path,
    )


@pytest.fixture
def clips(tmp_path):
    paths = []
    for i in range(2):
        p = tmp_path / f"clip{i}.mp4"
        p.write_bytes(b"\0" * 16)
        paths.append(p)
    return paths


@pytest.fixture
def stubs(monkeypatch):
    """Stand in for ffmpeg and the GPS parser; record what gets registered."""
    from gpstitch.services import merge_preparation as module

    registered = []
    manager = MagicMock()
    manager.add_file.side_effect = lambda **kwargs: registered.append(kwargs)
    monkeypatch.setattr(module, "file_manager", manager)

    monkeypatch.setattr(module, "check_mergeable", lambda paths, scratch_dir: 32)
    monkeypatch.setattr(
        module,
        "join_clips",
        lambda paths, output, on_progress=None, on_process=None: (output.write_bytes(b"\0"), output)[1],
    )
    monkeypatch.setattr(module, "clip_duration_seconds", lambda path: 10.0)
    monkeypatch.setattr(module, "parse_dji_meta_file", lambda path, on_progress=None: ["point"])
    monkeypatch.setattr(module, "points_on_joined_timeline", lambda segments: ["point"])
    monkeypatch.setattr(module, "derive_sample_rate", lambda points, target_hz: 1)
    monkeypatch.setattr(
        module,
        "dji_meta_to_gpx_file",
        lambda video, out, rate, points=None: (out.write_text("<gpx/>"), out)[1],
    )
    return registered


class TestPrepareMergedSource:
    def test_the_joined_video_becomes_the_primary(self, clips, tmp_path, stubs):
        from gpstitch.services.merge_preparation import prepare_merged_source

        prepare_merged_source(_config([str(c) for c in clips]), tmp_path, on_progress=lambda m: None)

        primary = [r for r in stubs if r["role"].value == "primary"]
        assert len(primary) == 1
        assert primary[0]["file_type"] == "video"

    def test_the_combined_gpx_becomes_the_secondary(self, clips, tmp_path, stubs):
        """Without it the renderer has no track at all for the joined file."""
        from gpstitch.services.merge_preparation import prepare_merged_source

        prepare_merged_source(_config([str(c) for c in clips]), tmp_path, on_progress=lambda m: None)

        secondary = [r for r in stubs if r["role"].value == "secondary"]
        assert len(secondary) == 1
        assert secondary[0]["file_type"] == "gpx"

    def test_both_temp_files_are_returned_for_cleanup(self, clips, tmp_path, stubs):
        from gpstitch.services.merge_preparation import prepare_merged_source

        temps = prepare_merged_source(_config([str(c) for c in clips]), tmp_path, on_progress=lambda m: None)

        assert any(t.endswith(".mp4") for t in temps)
        assert any(t.endswith(".gpx") for t in temps)

    def test_progress_is_reported_for_each_stage(self, clips, tmp_path, stubs):
        """The join alone runs for a quarter of an hour."""
        from gpstitch.services.merge_preparation import prepare_merged_source

        messages = []
        prepare_merged_source(_config([str(c) for c in clips]), tmp_path, on_progress=messages.append)

        assert any("GPS" in m for m in messages), messages

    def test_a_refused_merge_propagates(self, clips, tmp_path, stubs, monkeypatch):
        from gpstitch.services import merge_preparation as module
        from gpstitch.services.merge_preparation import prepare_merged_source
        from gpstitch.services.video_merge import MergeNotPossible

        def refuse(paths, scratch_dir):
            raise MergeNotPossible("Clips differ in resolution")

        monkeypatch.setattr(module, "check_mergeable", refuse)

        with pytest.raises(MergeNotPossible, match="resolution"):
            prepare_merged_source(_config([str(c) for c in clips]), tmp_path, on_progress=lambda m: None)

    def test_nothing_is_joined_when_the_merge_is_refused(self, clips, tmp_path, stubs, monkeypatch):
        """Refusing after an eighteen-minute copy would defeat the point."""
        from gpstitch.services import merge_preparation as module
        from gpstitch.services.merge_preparation import prepare_merged_source
        from gpstitch.services.video_merge import MergeNotPossible

        joined = []
        monkeypatch.setattr(module, "check_mergeable", MagicMock(side_effect=MergeNotPossible("no")))
        monkeypatch.setattr(module, "join_clips", lambda *a, **k: joined.append(1))

        with pytest.raises(MergeNotPossible):
            prepare_merged_source(_config([str(c) for c in clips]), tmp_path, on_progress=lambda m: None)

        assert joined == []

    def test_a_shared_gpx_skips_gps_extraction(self, clips, tmp_path, stubs, monkeypatch):
        """A supplied track wins, so there is nothing to extract."""
        from gpstitch.services import merge_preparation as module
        from gpstitch.services.merge_preparation import prepare_merged_source

        called = []
        monkeypatch.setattr(module, "parse_dji_meta_file", lambda path, on_progress=None: called.append(path) or [])

        shared = tmp_path / "ride.gpx"
        shared.write_text("<gpx/>")

        prepare_merged_source(
            _config([str(c) for c in clips], shared_gpx_path=str(shared)),
            tmp_path,
            on_progress=lambda m: None,
        )

        assert called == []

    def test_a_shared_gpx_is_not_deleted_afterwards(self, clips, tmp_path, stubs):
        """It is the user's file, not a temporary the job created."""
        from gpstitch.services.merge_preparation import prepare_merged_source

        shared = tmp_path / "ride.gpx"
        shared.write_text("<gpx/>")

        temps = prepare_merged_source(
            _config([str(c) for c in clips], shared_gpx_path=str(shared)),
            tmp_path,
            on_progress=lambda m: None,
        )

        assert str(shared) not in temps

    def test_clips_without_any_gps_are_refused(self, clips, tmp_path, stubs, monkeypatch):
        from gpstitch.services import merge_preparation as module
        from gpstitch.services.merge_preparation import prepare_merged_source

        monkeypatch.setattr(module, "points_on_joined_timeline", lambda segments: [])

        with pytest.raises(ValueError, match="GPS"):
            prepare_merged_source(_config([str(c) for c in clips]), tmp_path, on_progress=lambda m: None)

    def test_the_running_join_is_handed_out(self, clips, tmp_path, stubs, monkeypatch):
        """Cancelling has to be able to kill it rather than wait it out."""
        from gpstitch.services import merge_preparation as module
        from gpstitch.services.merge_preparation import prepare_merged_source

        def fake_join(paths, output, on_progress=None, on_process=None):
            output.write_bytes(b"\0")
            if on_process:
                on_process("the-process")
            return output

        monkeypatch.setattr(module, "join_clips", fake_join)

        seen = []
        prepare_merged_source(
            _config([str(c) for c in clips]), tmp_path, on_progress=lambda m: None, on_process=seen.append
        )

        assert seen == ["the-process"]


class TestPercentageReporter:
    """ffmpeg reports its position constantly; the job log keeps 500 lines.

    Reporting every update would flood the log and push the useful lines out, so
    progress is only announced when it has moved a visible amount.
    """

    def test_progress_is_reported_as_a_percentage(self):
        from gpstitch.services.merge_preparation import _percentage_reporter

        lines = []
        report = _percentage_reporter("Reading GPS from a.mp4", 100.0, lines.append)

        report(50.0)

        assert lines == ["Reading GPS from a.mp4 — 50%"]

    def test_small_advances_are_not_announced(self):
        from gpstitch.services.merge_preparation import _percentage_reporter

        lines = []
        report = _percentage_reporter("Reading", 100.0, lines.append)

        report(10.0)
        report(11.0)
        report(12.0)

        assert lines == ["Reading — 10%"]

    def test_each_visible_step_is_announced_once(self):
        from gpstitch.services.merge_preparation import _percentage_reporter

        lines = []
        report = _percentage_reporter("Reading", 100.0, lines.append)

        for second in range(0, 101, 5):
            report(float(second))

        assert lines == [f"Reading — {p}%" for p in range(0, 101, 10)]

    def test_an_unknown_duration_gives_no_reporter(self):
        """There is nothing to take a percentage of, so the extraction stays on
        its quieter path rather than dividing by zero."""
        from gpstitch.services.merge_preparation import _percentage_reporter

        assert _percentage_reporter("Reading", 0.0, [].append) is None

    def test_it_never_claims_more_than_a_hundred(self):
        """ffmpeg can overshoot slightly at the end of a stream."""
        from gpstitch.services.merge_preparation import _percentage_reporter

        lines = []
        report = _percentage_reporter("Reading", 10.0, lines.append)

        report(12.0)

        assert lines == ["Reading — 100%"]
