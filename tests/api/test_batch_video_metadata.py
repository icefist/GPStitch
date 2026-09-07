"""Batch must extract video metadata like the single-render path does.

Batch created its sessions without it, so primary.video_metadata was None for
every job. Four checks of the form getattr(primary.video_metadata,
"has_dji_meta", False) then silently evaluated False, and the command went out
without --ts-dji-meta-source - so gopro-dashboard looked for GoPro GPMF in a
DJI file and failed with "is it a GoPro file". The same clips previewed fine,
because preview loads GPS in-process with its own DJI fallback.
"""

from unittest.mock import patch

import pytest

from gpstitch.models.schemas import VideoMetadata


def _dji_metadata():
    return VideoMetadata(
        width=2688,
        height=1512,
        duration_seconds=60.0,
        frame_count=1500,
        frame_rate=25.0,
        has_gps=False,
        has_dji_meta=True,
    )


@pytest.fixture
def batch_videos(temp_dir):
    videos = []
    for i in range(2):
        path = temp_dir / f"DJI_000{i}.MP4"
        path.write_bytes(b"fake")
        videos.append(path)
    return videos


class TestBatchExtractsMetadata:
    @pytest.mark.anyio
    async def test_primary_file_carries_video_metadata(self, async_client, batch_videos):
        with patch("gpstitch.api.render.extract_video_metadata", return_value=_dji_metadata()):
            response = await async_client.post(
                "/api/render/batch",
                json={"files": [{"video_path": str(v)} for v in batch_videos]},
            )
        assert response.status_code == 200

        from gpstitch.services.file_manager import file_manager
        from gpstitch.services.job_manager import job_manager

        for job_id in response.json()["job_ids"]:
            job = await job_manager.get_job(job_id)
            primary = file_manager.get_primary_file(job.config.session_id)
            assert primary.video_metadata is not None, "batch job has no video metadata"
            assert primary.video_metadata.has_dji_meta is True

    @pytest.mark.anyio
    async def test_dji_video_uses_its_embedded_gps(self, async_client, batch_videos, temp_dir):
        """The failing command had neither --use-gpx-only nor the DJI source.

        With metadata present the builder takes its DJI branch, converting the
        embedded GPS to a temporary GPX and rendering from that instead of
        asking gopro-dashboard for GoPro GPMF the file does not have.
        """
        from gpstitch.services.renderer import DjiTrackSummary, generate_cli_command

        with patch("gpstitch.api.render.extract_video_metadata", return_value=_dji_metadata()):
            response = await async_client.post(
                "/api/render/batch",
                json={"files": [{"video_path": str(batch_videos[0])}]},
            )
        assert response.status_code == 200

        from gpstitch.services.job_manager import job_manager

        job = await job_manager.get_job(response.json()["job_ids"][0])

        fake_gpx = temp_dir / "converted.gpx"
        fake_gpx.write_text('<?xml version="1.0"?><gpx></gpx>', encoding="utf-8")
        summary = DjiTrackSummary(point_count=100, duration_s=100.0, position_frozen=False)
        with patch("gpstitch.services.renderer._convert_dji_meta_to_gpx", return_value=(str(fake_gpx), summary)):
            command, _ = generate_cli_command(
                session_id=job.config.session_id,
                output_file="/tmp/out.mp4",
                layout="default-1920x1080",
            )
        assert "--use-gpx-only" in command
        assert str(fake_gpx) in command

    @pytest.mark.anyio
    async def test_a_failing_probe_does_not_skip_the_file(self, async_client, batch_videos):
        """Metadata is a nicety; a broken probe must not drop the render."""
        with patch(
            "gpstitch.api.render.extract_video_metadata",
            side_effect=RuntimeError("ffprobe exploded"),
        ):
            response = await async_client.post(
                "/api/render/batch",
                json={"files": [{"video_path": str(v)} for v in batch_videos]},
            )
        assert response.status_code == 200
        assert response.json()["total_jobs"] == 2

    @pytest.mark.anyio
    async def test_non_video_inputs_are_not_probed(self, async_client, temp_dir):
        """A GPX primary has no video metadata to extract."""
        gpx = temp_dir / "track.gpx"
        gpx.write_text('<?xml version="1.0"?><gpx></gpx>', encoding="utf-8")

        with patch("gpstitch.api.render.extract_video_metadata") as probe:
            await async_client.post("/api/render/batch", json={"files": [{"video_path": str(gpx)}]})
        probe.assert_not_called()
