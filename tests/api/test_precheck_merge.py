"""Pre-check has to describe the render that will actually happen.

With merging on there is one output, so warning about three files that would be
overwritten - none of which will be written - is worse than saying nothing.
"""


def _videos(tmp_path, count=3):
    paths = []
    for i in range(count):
        p = tmp_path / f"DJI_004{i}_D.MP4"
        p.write_bytes(b"\0" * 16)
        paths.append(p)
    return paths


def _request(paths, **extra):
    return {"files": [{"video_path": str(p)} for p in paths], **extra}


class TestMergedPreCheck:
    async def test_one_planned_output_for_a_merged_batch(self, async_client, tmp_path):
        paths = _videos(tmp_path, count=3)

        response = await async_client.post("/api/render/pre-check", json=_request(paths, merge=True))

        assert response.status_code == 200, response.text
        assert len(response.json()["duplicate_outputs"]) == 0

    async def test_an_existing_merged_output_is_reported_once(self, async_client, tmp_path):
        """Three clips writing one file is one conflict, not three."""
        paths = _videos(tmp_path, count=3)
        (tmp_path / "DJI_0040_D_merged_overlay.mp4").write_bytes(b"\0")

        response = await async_client.post("/api/render/pre-check", json=_request(paths, merge=True))

        conflicts = response.json()["overwrite_conflicts"]
        assert len(conflicts) == 1
        assert "DJI_0040_D_merged_overlay" in conflicts[0]["output_path"]

    async def test_the_per_file_outputs_are_not_reported_when_merging(self, async_client, tmp_path):
        """Those files will never be written, so a warning about them is noise."""
        paths = _videos(tmp_path, count=2)
        (tmp_path / "DJI_0041_D_overlay.mp4").write_bytes(b"\0")

        response = await async_client.post("/api/render/pre-check", json=_request(paths, merge=True))

        assert response.json()["overwrite_conflicts"] == []

    async def test_without_merging_each_file_is_planned_separately(self, async_client, tmp_path):
        paths = _videos(tmp_path, count=3)
        (tmp_path / "DJI_0041_D_overlay.mp4").write_bytes(b"\0")

        response = await async_client.post("/api/render/pre-check", json=_request(paths))

        assert len(response.json()["overwrite_conflicts"]) == 1

    async def test_gps_quality_is_still_reported_for_every_clip(self, async_client, tmp_path):
        """Merging does not make a clip's GPS less worth checking."""
        paths = _videos(tmp_path, count=3)

        response = await async_client.post("/api/render/pre-check", json=_request(paths, merge=True))

        assert len(response.json()["gps_files"]) == 3

    async def test_a_single_clip_is_planned_as_itself(self, async_client, tmp_path):
        """One clip is not merged, so its output keeps the ordinary name."""
        paths = _videos(tmp_path, count=1)
        (tmp_path / "DJI_0040_D_overlay.mp4").write_bytes(b"\0")

        response = await async_client.post("/api/render/pre-check", json=_request(paths, merge=True))

        assert len(response.json()["overwrite_conflicts"]) == 1
