"""Slow endpoint work must not run on the event loop.

Reading a DJI clip's GPS stream takes ~107s and ffprobe on a 16GB file over USB
takes seconds. Called straight from a coroutine, that stalls the whole server:
no status poll, no log fetch, no cancel, and no progress indicator can even
animate while it happens. `preview.py` and `time_sync.py` already offload; these
did not.

Each test hands the endpoint a fake that blocks until a coroutine scheduled on
the loop releases it. If the work runs on the loop, that coroutine never gets to
run and the fake times out.
"""

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest


class LoopProbe:
    """A blocking stand-in that records whether the event loop kept running."""

    def __init__(self):
        self.released = threading.Event()
        self.loop_ran: bool | None = None

    def block(self):
        """Called from whatever thread the work runs on."""
        self.loop_ran = self.released.wait(timeout=5.0)

    async def release_after_a_tick(self):
        await asyncio.sleep(0.01)
        self.released.set()

    def assert_loop_kept_running(self):
        assert self.loop_ran is True, "the event loop was blocked while this endpoint worked"


@pytest.fixture
def probe():
    return LoopProbe()


class TestCommandEndpoint:
    """POST /api/command builds the whole render command.

    For a DJI clip that means reading the entire GPS stream and writing a GPX -
    minutes of work behind a button that showed nothing at all.
    """

    async def test_command_generation_is_off_the_loop(self, async_client, probe, monkeypatch):
        from gpstitch.services import file_manager as file_manager_module

        manager = MagicMock()
        manager.session_exists.return_value = True
        manager.get_primary_file.return_value = MagicMock(file_path="/tmp/v.mp4")
        monkeypatch.setattr(file_manager_module, "file_manager", manager)
        monkeypatch.setattr("gpstitch.api.command.file_manager", manager)

        def fake_generate(**kwargs):
            probe.block()
            return ("gpstitch-dashboard /tmp/out.mp4", [])

        with patch("gpstitch.api.command.generate_cli_command", side_effect=fake_generate):
            request = async_client.post(
                "/api/command",
                json={"session_id": "s1", "layout": "default-1920x1080", "output_filename": "/tmp/out.mp4"},
            )
            response, _ = await asyncio.gather(request, probe.release_after_a_tick())

        probe.assert_loop_kept_running()
        assert response.status_code == 200


class TestBatchEndpoints:
    """Batch creation and pre-check run ffprobe once per file."""

    async def test_pre_check_is_off_the_loop(self, async_client, probe, tmp_path):
        """Pre-check reads GPS quality for every file in the batch."""
        video = tmp_path / "a.mp4"
        video.write_bytes(b"fake")

        def fake_analyze(path):
            probe.block()
            return None

        with patch("gpstitch.services.gps_analyzer.analyze_gps_quality", side_effect=fake_analyze):
            request = async_client.post(
                "/api/render/pre-check",
                json={"files": [{"video_path": str(video)}], "output_dir": str(tmp_path)},
            )
            response, _ = await asyncio.gather(request, probe.release_after_a_tick())

        probe.assert_loop_kept_running()
        assert response.status_code == 200

    async def test_batch_creation_is_off_the_loop(self, async_client, probe, tmp_path):
        """Creating a batch reads metadata from every video before it responds."""
        video = tmp_path / "a.mp4"
        video.write_bytes(b"fake")

        def fake_metadata(path):
            probe.block()
            return MagicMock(has_dji_meta=False, duration_seconds=1.0, frame_rate=30.0)

        with patch("gpstitch.api.render.extract_video_metadata", side_effect=fake_metadata):
            request = async_client.post(
                "/api/render/batch",
                json={"files": [{"video_path": str(video)}], "layout": "default-1920x1080"},
            )
            response, _ = await asyncio.gather(request, probe.release_after_a_tick())

        probe.assert_loop_kept_running()
        assert response.status_code in (200, 400), response.text


class TestLocalFileEndpoint:
    """Selecting a local file runs ffprobe and a GPS quality pass.

    ffprobe on a 16GB clip over USB takes seconds on first touch, and the Load
    button is showing "Reading..." while it happens - which it cannot do if the
    server is stalled.
    """

    async def test_metadata_extraction_is_off_the_loop(self, async_client, probe, tmp_path):
        video = tmp_path / "a.mp4"
        video.write_bytes(b"fake")

        from tests.fixtures.factories import create_video_metadata

        def fake_metadata(path):
            probe.block()
            return create_video_metadata(has_gps=False)

        with (
            patch("gpstitch.api.upload.extract_video_metadata", side_effect=fake_metadata),
            patch("gpstitch.api.upload.analyze_gps_quality", return_value=None),
        ):
            request = async_client.post("/api/local-file", json={"file_path": str(video)})
            response, _ = await asyncio.gather(request, probe.release_after_a_tick())

        probe.assert_loop_kept_running()
        assert response.status_code in (200, 400), response.text
