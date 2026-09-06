"""output_dir on the render requests.

The output extension depends on the ffmpeg profile, and that mapping lives in
the backend - so the caller sends a folder and the backend names the file,
rather than the frontend duplicating logic that would drift.
"""

import os
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def video_session(tmp_path):
    """A session whose primary file is a video, plus an output folder."""
    from gpstitch.models.schemas import FileRole
    from gpstitch.services.file_manager import file_manager

    src = tmp_path / "shoot"
    out = tmp_path / "exports"
    src.mkdir()
    out.mkdir()
    video = src / "DJI_0001.mp4"
    video.write_bytes(b"fake")

    session_id = file_manager.create_local_session()
    file_manager.add_file(
        session_id=session_id,
        filename=video.name,
        file_path=str(video),
        file_type="video",
        role=FileRole.PRIMARY,
    )
    return session_id, video, out


class TestSingleRenderOutputDir:
    async def test_output_lands_in_the_chosen_folder(self, async_client, video_session):
        session_id, _, out = video_session
        with patch("gpstitch.services.render_service.render_service.start_render", AsyncMock()):
            response = await async_client.post(
                "/api/render/start",
                json={"session_id": session_id, "output_dir": str(out)},
            )
        assert response.status_code == 200
        assert os.path.dirname(response.json()["output_file"]) == str(out)

    async def test_filename_still_derives_from_the_source(self, async_client, video_session):
        session_id, _, out = video_session
        with patch("gpstitch.services.render_service.render_service.start_render", AsyncMock()):
            response = await async_client.post(
                "/api/render/start",
                json={"session_id": session_id, "output_dir": str(out)},
            )
        assert os.path.basename(response.json()["output_file"]) == "DJI_0001_overlay.mp4"

    async def test_without_output_dir_it_lands_beside_the_source(self, async_client, video_session):
        session_id, video, _ = video_session
        with patch("gpstitch.services.render_service.render_service.start_render", AsyncMock()):
            response = await async_client.post("/api/render/start", json={"session_id": session_id})
        assert os.path.dirname(response.json()["output_file"]) == str(video.parent)

    async def test_an_explicit_output_file_still_wins(self, async_client, video_session):
        session_id, _, out = video_session
        explicit = str(out / "custom_name.mp4")
        with patch("gpstitch.services.render_service.render_service.start_render", AsyncMock()):
            response = await async_client.post(
                "/api/render/start",
                json={
                    "session_id": session_id,
                    "output_dir": "/somewhere/else",
                    "output_file": explicit,
                },
            )
        assert response.json()["output_file"] == explicit
