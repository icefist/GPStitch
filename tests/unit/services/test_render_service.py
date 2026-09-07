"""Unit tests for render_service - process cancellation, cleanup, and mtime alignment."""

import asyncio
import datetime
import signal
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestCancelRender:
    """Tests for cancel_render method - killing process groups."""

    @pytest.fixture
    def render_service(self):
        """Create a fresh RenderService instance."""
        # Import here to avoid side effects from patches
        from gpstitch.services.render_service import RenderService

        service = RenderService()
        return service

    @pytest.fixture
    def mock_process(self):
        """Create a mock subprocess."""
        process = MagicMock()
        process.pid = 12345
        process.wait = AsyncMock(return_value=0)
        return process

    async def test_cancel_render_wrong_job_id(self, render_service):
        """Cancel returns False if job_id doesn't match current job."""
        render_service._current_job_id = "job-123"

        result = await render_service.cancel_render("job-456")

        assert result is False

    async def test_cancel_render_no_process(self, render_service):
        """Cancel returns False if no process is running."""
        render_service._current_job_id = "job-123"
        render_service._process = None

        result = await render_service.cancel_render("job-123")

        assert result is False

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix-specific test")
    async def test_cancel_render_kills_process_group_unix(self, render_service, mock_process):
        """On Unix, cancel_render should kill entire process group."""
        render_service._current_job_id = "job-123"
        render_service._process = mock_process

        with (
            patch("os.killpg") as mock_killpg,
            patch("gpstitch.services.render_service.job_manager") as mock_job_manager,
        ):
            mock_job_manager.update_job_status = AsyncMock()

            result = await render_service.cancel_render("job-123")

            assert result is True
            # Should call killpg with SIGTERM first
            mock_killpg.assert_called_with(12345, signal.SIGTERM)

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix-specific test")
    async def test_cancel_render_force_kills_on_timeout(self, render_service, mock_process):
        """On timeout, cancel_render should force kill with SIGKILL."""
        render_service._current_job_id = "job-123"
        render_service._process = mock_process

        # Make wait() timeout
        async def slow_wait():
            await asyncio.sleep(10)

        mock_process.wait = slow_wait

        with (
            patch("os.killpg") as mock_killpg,
            patch("gpstitch.services.render_service.job_manager") as mock_job_manager,
        ):
            mock_job_manager.update_job_status = AsyncMock()

            # Use shorter timeout for test
            with patch("asyncio.wait_for", side_effect=TimeoutError):
                # Create a fast completing wait for after SIGKILL
                mock_process.wait = AsyncMock(return_value=0)

                result = await render_service.cancel_render("job-123")

            assert result is True
            # Should have called killpg twice: SIGTERM then SIGKILL
            calls = mock_killpg.call_args_list
            assert len(calls) >= 1
            # Last call should be SIGKILL
            assert any(call[0][1] == signal.SIGKILL for call in calls)

    async def test_cancel_render_handles_process_already_dead(self, render_service, mock_process):
        """Cancel should handle ProcessLookupError gracefully."""
        render_service._current_job_id = "job-123"
        render_service._process = mock_process

        with (
            patch("os.killpg", side_effect=ProcessLookupError),
            patch("gpstitch.services.render_service.job_manager") as mock_job_manager,
        ):
            mock_job_manager.update_job_status = AsyncMock()

            result = await render_service.cancel_render("job-123")

            # Should still return True - process is dead
            assert result is True


class TestKillProcessTree:
    """Tests for _kill_process_tree helper method."""

    @pytest.fixture
    def render_service(self):
        """Create a fresh RenderService instance."""
        from gpstitch.services.render_service import RenderService

        return RenderService()

    @pytest.fixture
    def mock_process(self):
        """Create a mock subprocess."""
        process = MagicMock()
        process.pid = 12345
        process.wait = AsyncMock(return_value=0)
        process.kill = MagicMock()
        return process

    async def test_kill_process_tree_no_process(self, render_service):
        """Should do nothing if no process exists."""
        render_service._process = None

        # Should not raise
        await render_service._kill_process_tree()

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix-specific test")
    async def test_kill_process_tree_unix(self, render_service, mock_process):
        """On Unix, should kill entire process group with SIGKILL."""
        render_service._process = mock_process

        with patch("os.killpg") as mock_killpg:
            await render_service._kill_process_tree()

            mock_killpg.assert_called_once_with(12345, signal.SIGKILL)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific test")
    async def test_kill_process_tree_windows(self, render_service, mock_process):
        """On Windows, should call process.kill()."""
        render_service._process = mock_process

        await render_service._kill_process_tree()

        mock_process.kill.assert_called_once()

    async def test_kill_process_tree_handles_already_dead(self, render_service, mock_process):
        """Should handle ProcessLookupError gracefully."""
        render_service._process = mock_process

        with patch("os.killpg", side_effect=ProcessLookupError):
            # Should not raise
            await render_service._kill_process_tree()


class TestResolveMtimeForAlignment:
    """Tests for _resolve_mtime_for_alignment method."""

    @pytest.fixture
    def render_service(self):
        from gpstitch.services.render_service import RenderService

        return RenderService()

    @pytest.fixture
    def config(self):
        from gpstitch.models.job import RenderJobConfig

        return RenderJobConfig(
            session_id="test-session",
            layout="default-1920x1080",
            output_file="/tmp/output.mp4",
        )

    def test_auto_mode_with_creation_time(self, render_service, config):
        """Auto mode should return creation_time as Unix timestamp."""
        config.video_time_alignment = "auto"
        creation_time = datetime.datetime(2024, 8, 8, 17, 13, 0, tzinfo=datetime.UTC)

        with patch(
            "gpstitch.services.renderer._extract_creation_time",
            return_value=creation_time,
        ):
            ts = render_service._resolve_mtime_for_alignment(config, "/tmp/video.mov")

        assert ts == creation_time.timestamp()

    def test_auto_mode_fallback_to_ctime(self, render_service, config):
        """Auto mode should fallback to filestat().ctime when no creation_time."""
        config.video_time_alignment = "auto"

        fake_ctime = datetime.datetime(2024, 8, 8, 17, 13, 0, tzinfo=datetime.UTC)
        mock_fstat = SimpleNamespace(ctime=fake_ctime)

        with (
            patch(
                "gpstitch.services.renderer._extract_creation_time",
                return_value=None,
            ),
            patch("gopro_overlay.ffmpeg_gopro.filestat", return_value=mock_fstat),
        ):
            ts = render_service._resolve_mtime_for_alignment(config, "/tmp/video.mov")

        assert ts == fake_ctime.timestamp()

    def test_manual_mode_with_offset(self, render_service, config):
        """Manual mode should add offset to creation_time timestamp."""
        config.video_time_alignment = "manual"
        config.time_offset_seconds = 60
        creation_time = datetime.datetime(2024, 8, 8, 17, 13, 0, tzinfo=datetime.UTC)

        with patch(
            "gpstitch.services.renderer._extract_creation_time",
            return_value=creation_time,
        ):
            ts = render_service._resolve_mtime_for_alignment(config, "/tmp/video.mov")

        assert ts == creation_time.timestamp() + 60

    def test_manual_mode_with_negative_offset(self, render_service, config):
        """Manual mode should support negative offsets."""
        config.video_time_alignment = "manual"
        config.time_offset_seconds = -30
        creation_time = datetime.datetime(2024, 8, 8, 17, 13, 0, tzinfo=datetime.UTC)

        with patch(
            "gpstitch.services.renderer._extract_creation_time",
            return_value=creation_time,
        ):
            ts = render_service._resolve_mtime_for_alignment(config, "/tmp/video.mov")

        assert ts == creation_time.timestamp() - 30

    def test_gpx_timestamps_returns_none(self, render_service, config):
        """GPX-timestamps mode should return None (no mtime change needed)."""
        config.video_time_alignment = "gpx-timestamps"

        ts = render_service._resolve_mtime_for_alignment(config, "/tmp/video.mov")

        assert ts is None

    def test_none_alignment_returns_none(self, render_service, config):
        """No alignment should return None."""
        config.video_time_alignment = None

        ts = render_service._resolve_mtime_for_alignment(config, "/tmp/video.mov")

        assert ts is None

    def test_file_modified_with_gpx_secondary(self, render_service, config, monkeypatch):
        """file-modified mode with GPX secondary should use GPX start timestamp."""
        config.video_time_alignment = "file-modified"

        mock_secondary = MagicMock()
        mock_secondary.file_type = "gpx"
        mock_secondary.file_path = "/tmp/track.gpx"

        from gpstitch.services import file_manager as fm_module

        mock_fm = MagicMock()
        mock_fm.get_secondary_file.return_value = mock_secondary
        monkeypatch.setattr(fm_module, "file_manager", mock_fm)

        with patch.object(render_service, "_get_gpx_start_timestamp", return_value=1723132380.0):
            ts = render_service._resolve_mtime_for_alignment(config, "/tmp/video.mov")

        assert ts == 1723132380.0

    def test_file_modified_with_srt_secondary(self, render_service, config, monkeypatch):
        """file-modified mode with SRT secondary should use original video mtime."""
        config.video_time_alignment = "file-modified"

        mock_secondary = MagicMock()
        mock_secondary.file_type = "srt"
        mock_secondary.file_path = "/tmp/telemetry.srt"

        from gpstitch.services import file_manager as fm_module

        mock_fm = MagicMock()
        mock_fm.get_secondary_file.return_value = mock_secondary
        monkeypatch.setattr(fm_module, "file_manager", mock_fm)

        mock_stat = MagicMock()
        mock_stat.st_mtime = 1723132380.0

        with patch("os.stat", return_value=mock_stat):
            ts = render_service._resolve_mtime_for_alignment(config, "/tmp/video.mov")

        assert ts == 1723132380.0


class TestNeedsPillarboxUsesSidecarCanvas:
    """_needs_pillarbox must respect canvas dimensions from a custom XML template's sidecar
    JSON. Without this, a custom 4K 4:3 template (3840x2880) was treated as the default
    built-in layout (1920x1080), which scaled the video down."""

    @pytest.fixture
    def render_service(self):
        from gpstitch.services.render_service import RenderService

        return RenderService()

    def _patch_video_probe(self, monkeypatch, width: int, height: int):
        """Stub gopro-overlay video probing to report the given display dimensions."""
        from gpstitch.services import metadata as metadata_mod

        monkeypatch.setattr(metadata_mod, "get_video_rotation", lambda _p: 0)
        monkeypatch.setattr(metadata_mod, "get_display_dimensions", lambda w, h, _r: (w, h))

        fake_rec = SimpleNamespace(video=SimpleNamespace(dimension=SimpleNamespace(x=width, y=height)))

        class FakeFFMPEGGoPro:
            def __init__(self, *_args, **_kwargs):
                pass

            def find_recording(self, _p):
                return fake_rec

        import gopro_overlay.ffmpeg as gp_ffmpeg
        import gopro_overlay.ffmpeg_gopro as gp_ffmpeg_gopro

        monkeypatch.setattr(gp_ffmpeg, "FFMPEG", lambda: MagicMock())
        monkeypatch.setattr(gp_ffmpeg_gopro, "FFMPEGGoPro", FakeFFMPEGGoPro)

    def test_custom_xml_4k_4_3_no_pillarbox(self, render_service, tmp_path, monkeypatch):
        """Video 3840x2880 with custom XML whose sidecar canvas is 3840x2880 → no pillarbox."""
        xml_path = tmp_path / "Osmo6_Walking_4k_4_3.xml"
        xml_path.write_text("<layout></layout>", encoding="utf-8")
        (tmp_path / "Osmo6_Walking_4k_4_3.json").write_text(
            '{"canvas_width": 3840, "canvas_height": 2880}', encoding="utf-8"
        )

        self._patch_video_probe(monkeypatch, 3840, 2880)

        config = MagicMock()
        config.layout = "xml"
        config.layout_xml_path = str(xml_path)

        result = render_service._needs_pillarbox("/fake/video.mp4", config)
        assert result is None, f"Expected no pillarbox (matching aspect), got {result}"

    def test_custom_xml_canvas_different_aspect_pillarboxes_to_sidecar(self, render_service, tmp_path, monkeypatch):
        """16:9 video into a 4:3 sidecar canvas must pillarbox to the sidecar's dims."""
        xml_path = tmp_path / "custom_4_3.xml"
        xml_path.write_text("<layout></layout>", encoding="utf-8")
        (tmp_path / "custom_4_3.json").write_text('{"canvas_width": 3840, "canvas_height": 2880}', encoding="utf-8")

        self._patch_video_probe(monkeypatch, 3840, 2160)

        config = MagicMock()
        config.layout = "xml"
        config.layout_xml_path = str(xml_path)

        result = render_service._needs_pillarbox("/fake/video.mp4", config)
        assert result is not None
        canvas_w, canvas_h, video_w, video_h = result
        assert (canvas_w, canvas_h) == (3840, 2880)
        assert (video_w, video_h) == (3840, 2160)


class TestDjiMetaMtimeAlignment:
    """mtime for a DJI clip comes from its first GPS point.

    Reading the whole stream to reach points[0] means seeking through the entire
    file - 107s measured on a 12GB clip - and generate_cli_command already paid
    that once for the GPX conversion. first_dji_meta_point reads a window from
    the start instead.
    """

    @pytest.fixture
    def render_service(self):
        from gpstitch.services.render_service import RenderService

        return RenderService()

    @pytest.fixture
    def config(self):
        from gpstitch.models.job import RenderJobConfig

        return RenderJobConfig(
            session_id="s1",
            layout="default-1920x1080",
            output_file="/tmp/out.mp4",
            video_time_alignment="auto",
        )

    def _dji_session(self, monkeypatch, frame_idx=0, fps=29.97):
        """A session whose primary is a DJI clip with embedded GPS."""
        from gpstitch.services import render_service as module

        primary = SimpleNamespace(
            file_type="video",
            file_path="/tmp/dji.mp4",
            video_metadata=SimpleNamespace(has_dji_meta=True, frame_rate=fps),
        )
        fake_manager = SimpleNamespace(
            get_primary_file=lambda _s: primary,
            get_secondary_file=lambda _s: None,
        )
        monkeypatch.setattr(module, "file_manager", fake_manager, raising=False)
        monkeypatch.setitem(
            __import__("sys").modules,
            "gpstitch.services.file_manager",
            SimpleNamespace(file_manager=fake_manager),
        )
        return primary

    def test_the_whole_stream_is_not_parsed(self, render_service, config, monkeypatch):
        self._dji_session(monkeypatch)
        point = SimpleNamespace(timestamp=datetime.datetime(2026, 9, 6, 18, 25, 14), frame_idx=0)

        with (
            patch("gpstitch.services.dji_meta_parser.first_dji_meta_point", return_value=point) as cheap,
            patch("gpstitch.services.dji_meta_parser.parse_dji_meta_file") as expensive,
        ):
            result = render_service._resolve_mtime_for_alignment(config, "/tmp/dji.mp4")

        assert cheap.called, "should read only the first point"
        assert not expensive.called, "must not seek through the whole stream"
        assert result == datetime.datetime(2026, 9, 6, 18, 25, 14, tzinfo=datetime.UTC).timestamp()

    def test_a_late_gps_lock_is_subtracted(self, render_service, config, monkeypatch):
        """points[0].frame_idx > 0 means GPS locked after recording began."""
        self._dji_session(monkeypatch, fps=30.0)
        point = SimpleNamespace(timestamp=datetime.datetime(2026, 9, 6, 18, 25, 14), frame_idx=600)

        with patch("gpstitch.services.dji_meta_parser.first_dji_meta_point", return_value=point):
            result = render_service._resolve_mtime_for_alignment(config, "/tmp/dji.mp4")

        expected = datetime.datetime(2026, 9, 6, 18, 25, 14, tzinfo=datetime.UTC).timestamp() - 20.0
        assert result == expected

    def test_no_gps_point_falls_through(self, render_service, config, monkeypatch):
        """No first point means no DJI alignment; the caller's other modes apply."""
        self._dji_session(monkeypatch)

        with (
            patch("gpstitch.services.dji_meta_parser.first_dji_meta_point", return_value=None),
            patch("gpstitch.services.renderer._extract_creation_time", return_value=None),
            patch("gopro_overlay.ffmpeg_gopro.filestat") as filestat,
        ):
            filestat.return_value = SimpleNamespace(ctime=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC))
            result = render_service._resolve_mtime_for_alignment(config, "/tmp/dji.mp4")

        assert result == datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC).timestamp()


class TestPreparationPhase:
    """What happens before the render subprocess exists.

    generate_cli_command reads the whole GPS stream to build a GPX - ~107s on a
    12GB clip. It is synchronous, and was called directly from this coroutine, so
    it blocked the event loop for that whole time: no status poll, no log fetch
    and no cancel request could be served. The job also stayed PENDING with an
    empty log, and cancel refused because no subprocess existed yet.
    """

    @pytest.fixture
    def render_service(self):
        from gpstitch.services.render_service import RenderService

        return RenderService()

    @pytest.fixture
    def config(self):
        from gpstitch.models.job import RenderJobConfig

        return RenderJobConfig(
            session_id="s1",
            layout="default-1920x1080",
            output_file="/tmp/out.mp4",
            video_time_alignment="none",
        )

    @pytest.fixture
    def harness(self, render_service, monkeypatch):
        """Stub everything around command generation; record status and log calls."""
        import sys as _sys

        from gpstitch.services import render_service as module

        statuses = []
        log_lines = []

        jm = MagicMock()
        jm.update_job_status = AsyncMock(side_effect=lambda jid, st, err=None: statuses.append(st))
        jm.append_job_log = AsyncMock(side_effect=lambda jid, line: log_lines.append(line))
        jm.update_job_progress = AsyncMock()
        jm.set_job_pid = AsyncMock()
        jm.get_job = AsyncMock(return_value=None)
        monkeypatch.setattr(module, "job_manager", jm)

        # No primary file: skips pillarbox and mtime handling entirely.
        empty_manager = SimpleNamespace(get_primary_file=lambda _s: None, get_secondary_file=lambda _s: None)
        monkeypatch.setitem(_sys.modules, "gpstitch.services.file_manager", SimpleNamespace(file_manager=empty_manager))

        monkeypatch.setattr(render_service, "_find_gopro_dashboard", lambda: "/usr/bin/gopro-dashboard.py")
        monkeypatch.setattr(render_service, "_stream_output", AsyncMock())
        monkeypatch.setattr(render_service, "_start_next_pending_job", AsyncMock())

        spawned = []

        async def fake_exec(*args, **kwargs):
            spawned.append(args)
            proc = MagicMock()
            proc.pid = 4242
            proc.wait = AsyncMock(return_value=0)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        return SimpleNamespace(statuses=statuses, log_lines=log_lines, spawned=spawned, job_manager=jm)

    async def test_command_generation_does_not_block_the_event_loop(self, render_service, config, harness, monkeypatch):
        """A coroutine scheduled during generation must actually get to run."""
        import threading

        from gpstitch.services import render_service as module

        released = threading.Event()
        observed = {}

        def fake_generate(**kwargs):
            observed["loop_ran"] = released.wait(timeout=5.0)
            return ("gpstitch-dashboard /tmp/out.mp4", [])

        monkeypatch.setattr(module, "generate_cli_command", fake_generate)

        async def release():
            await asyncio.sleep(0.01)
            released.set()

        await asyncio.gather(render_service.start_render("job-1", config), release())

        assert observed["loop_ran"] is True, "the event loop was blocked during command generation"

    async def test_the_job_is_marked_running_before_preparation(self, render_service, config, harness, monkeypatch):
        from gpstitch.models.job import JobStatus
        from gpstitch.services import render_service as module

        order = []
        harness.job_manager.update_job_status = AsyncMock(
            side_effect=lambda jid, st, err=None: order.append(f"status:{st.value}")
        )

        def fake_generate(**kwargs):
            order.append("generate")
            return ("gpstitch-dashboard /tmp/out.mp4", [])

        monkeypatch.setattr(module, "generate_cli_command", fake_generate)

        await render_service.start_render("job-1", config)

        assert order.index(f"status:{JobStatus.RUNNING.value}") < order.index("generate")

    async def test_preparation_progress_reaches_the_job_log(self, render_service, config, harness, monkeypatch):
        from gpstitch.services import render_service as module

        def fake_generate(**kwargs):
            kwargs["on_progress"]("Extracting embedded GPS telemetry")
            kwargs["on_progress"]("Extracted 43917 GPS points in 107s")
            return ("gpstitch-dashboard /tmp/out.mp4", [])

        monkeypatch.setattr(module, "generate_cli_command", fake_generate)

        await render_service.start_render("job-1", config)

        assert "Extracting embedded GPS telemetry" in harness.log_lines
        assert "Extracted 43917 GPS points in 107s" in harness.log_lines

    async def test_cancel_is_accepted_while_preparing(self, render_service):
        """No subprocess exists yet, but the job is ours and must be stoppable."""
        render_service._current_job_id = "job-1"
        render_service._preparing_job_id = "job-1"
        render_service._process = None

        assert await render_service.cancel_render("job-1") is True

    async def test_a_cancelled_preparation_never_launches_the_render(
        self, render_service, config, harness, monkeypatch, tmp_path
    ):
        from gpstitch.models.job import JobStatus
        from gpstitch.services import render_service as module

        temp_gpx = tmp_path / "scratch.gpx"
        temp_gpx.write_text("<gpx/>", encoding="utf-8")
        loop = asyncio.get_running_loop()

        def fake_generate(**kwargs):
            # The cancel request arrives mid-extraction, from the event loop.
            future = asyncio.run_coroutine_threadsafe(render_service.cancel_render("job-1"), loop)
            assert future.result(timeout=5.0) is True
            return ("gpstitch-dashboard /tmp/out.mp4", [str(temp_gpx)])

        monkeypatch.setattr(module, "generate_cli_command", fake_generate)

        await render_service.start_render("job-1", config)

        assert harness.spawned == [], "a cancelled job must not start the render subprocess"
        assert JobStatus.CANCELLED in harness.statuses
        assert not temp_gpx.exists(), "temp files from the abandoned preparation should be cleaned up"

    async def test_a_cancel_landing_after_preparation_still_prevents_the_launch(
        self, render_service, config, harness, monkeypatch
    ):
        """Preparation is not the only slow step before the subprocess exists.

        Pillarboxing re-encodes the whole video, and mtime alignment reads the
        stream, so a cancel can arrive after the command is built but before
        anything is launched. It must be honoured there too.
        """
        from gpstitch.services import render_service as module

        monkeypatch.setattr(module, "generate_cli_command", lambda **kwargs: ("gpstitch-dashboard /tmp/out.mp4", []))

        def cancel_then_resolve():
            render_service._cancelled_while_preparing.add("job-1")
            return "/usr/bin/gopro-dashboard.py"

        monkeypatch.setattr(render_service, "_find_gopro_dashboard", cancel_then_resolve)

        await render_service.start_render("job-1", config)

        assert harness.spawned == [], "a cancelled job must not start the render subprocess"
        assert "Cancelled before rendering started" in harness.log_lines

    async def test_cancel_is_still_accepted_after_the_command_is_built(
        self, render_service, config, harness, monkeypatch
    ):
        """The preparing marker has to survive until a process exists."""
        from gpstitch.services import render_service as module

        accepted = {}

        monkeypatch.setattr(module, "generate_cli_command", lambda **kwargs: ("gpstitch-dashboard /tmp/out.mp4", []))

        def check_then_resolve():
            # Runs after the command is built, before the subprocess is created.
            accepted["preparing"] = render_service._preparing_job_id
            return "/usr/bin/gopro-dashboard.py"

        monkeypatch.setattr(render_service, "_find_gopro_dashboard", check_then_resolve)

        await render_service.start_render("job-1", config)

        assert accepted["preparing"] == "job-1", "cancel would be refused in this window"
