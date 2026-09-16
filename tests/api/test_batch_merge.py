"""Merging a batch produces one job, not one per file.

The point of the feature is a single rendered video whose overlay runs off one
continuous track. That only works if one job owns every clip.
"""

from pathlib import Path

from gpstitch.services.job_manager import job_manager


def _videos(tmp_path, count=3):
    paths = []
    for i in range(count):
        p = tmp_path / f"DJI_004{i}_D.MP4"
        p.write_bytes(b"\0" * 16)
        paths.append(p)
    return paths


def _request(paths, **extra):
    return {
        "files": [{"video_path": str(p)} for p in paths],
        "layout": "default-1920x1080",
        **extra,
    }


class TestMergedBatch:
    async def test_merging_creates_a_single_job(self, async_client, tmp_path):
        paths = _videos(tmp_path, count=3)

        response = await async_client.post("/api/render/batch", json=_request(paths, merge=True))

        assert response.status_code == 200, response.text
        assert response.json()["total_jobs"] == 1

    async def test_the_job_carries_every_clip_in_order(self, async_client, tmp_path):
        paths = _videos(tmp_path, count=3)

        response = await async_client.post("/api/render/batch", json=_request(paths, merge=True))

        job = await job_manager.get_job(response.json()["job_ids"][0])
        assert job.config.merge_sources == [str(p) for p in paths]

    async def test_without_merging_each_clip_gets_its_own_job(self, async_client, tmp_path):
        paths = _videos(tmp_path, count=3)

        response = await async_client.post("/api/render/batch", json=_request(paths))

        assert response.json()["total_jobs"] == 3

    async def test_a_single_clip_is_not_merged(self, async_client, tmp_path):
        """One clip needs no join, so it takes the ordinary path."""
        paths = _videos(tmp_path, count=1)

        response = await async_client.post("/api/render/batch", json=_request(paths, merge=True))

        job = await job_manager.get_job(response.json()["job_ids"][0])
        assert job.config.merge_sources is None

    async def test_the_output_is_named_after_the_first_clip(self, async_client, tmp_path):
        paths = _videos(tmp_path, count=2)

        response = await async_client.post("/api/render/batch", json=_request(paths, merge=True))

        job = await job_manager.get_job(response.json()["job_ids"][0])
        assert Path(job.config.output_file).name.startswith("DJI_0040_D_merged_overlay")

    async def test_the_output_folder_is_honoured(self, async_client, tmp_path):
        paths = _videos(tmp_path, count=2)
        destination = tmp_path / "out"
        destination.mkdir()

        response = await async_client.post(
            "/api/render/batch", json=_request(paths, merge=True, output_dir=str(destination))
        )

        job = await job_manager.get_job(response.json()["job_ids"][0])
        assert Path(job.config.output_file).parent == destination

    async def test_the_merged_job_aligns_from_its_gpx(self, async_client, tmp_path):
        """The joined file carries no embedded GPS, so its start comes from the track."""
        paths = _videos(tmp_path, count=2)

        response = await async_client.post("/api/render/batch", json=_request(paths, merge=True))

        job = await job_manager.get_job(response.json()["job_ids"][0])
        assert job.config.video_time_alignment == "file-modified"

    async def test_a_shared_gpx_is_carried_to_the_job(self, async_client, tmp_path):
        track = tmp_path / "ride.gpx"
        track.write_text("<gpx/>", encoding="utf-8")
        paths = _videos(tmp_path, count=2)

        response = await async_client.post(
            "/api/render/batch", json=_request(paths, merge=True, shared_gpx_path=str(track))
        )

        job = await job_manager.get_job(response.json()["job_ids"][0])
        assert job.config.shared_gpx_path == str(track)

    async def test_a_missing_clip_is_left_out_of_the_merge(self, async_client, tmp_path):
        paths = _videos(tmp_path, count=2)
        request = _request(paths, merge=True)
        request["files"].append({"video_path": str(tmp_path / "gone.mp4")})

        response = await async_client.post("/api/render/batch", json=request)

        job = await job_manager.get_job(response.json()["job_ids"][0])
        assert job.config.merge_sources == [str(p) for p in paths]
