"""Pre-check must reason about the folder we will actually write to.

Outputs used to land beside their source, so names could not collide. Once an
output folder is chosen, two same-named clips from different shoots both map to
one output file - which would silently destroy the first render.
"""

from pathlib import Path

import pytest


@pytest.fixture
def two_shoots(tmp_path):
    """Same filename in two source folders, plus a separate output folder."""
    a = tmp_path / "shoot-a"
    b = tmp_path / "shoot-b"
    out = tmp_path / "exports"
    for d in (a, b, out):
        d.mkdir()
    (a / "DJI_0001.mp4").write_bytes(b"fake")
    (b / "DJI_0001.mp4").write_bytes(b"fake")
    return a, b, out


class TestOutputDirRespected:
    async def test_conflict_is_reported_against_the_output_dir(self, async_client, two_shoots):
        a, _, out = two_shoots
        (out / "DJI_0001_overlay.mp4").write_bytes(b"existing")

        response = await async_client.post(
            "/api/render/pre-check",
            json={
                "files": [{"video_path": str(a / "DJI_0001.mp4")}],
                "output_dir": str(out),
            },
        )
        assert response.status_code == 200
        conflicts = response.json()["overwrite_conflicts"]
        assert len(conflicts) == 1
        assert conflicts[0]["output_path"] == str(out / "DJI_0001_overlay.mp4")

    async def test_no_conflict_when_only_the_source_dir_has_that_name(self, async_client, two_shoots):
        """The old behaviour would have warned here, about the wrong file."""
        a, _, out = two_shoots
        (a / "DJI_0001_overlay.mp4").write_bytes(b"beside the source")

        response = await async_client.post(
            "/api/render/pre-check",
            json={
                "files": [{"video_path": str(a / "DJI_0001.mp4")}],
                "output_dir": str(out),
            },
        )
        assert response.json()["overwrite_conflicts"] == []

    async def test_without_output_dir_the_source_folder_is_still_used(self, async_client, two_shoots):
        a, _, _ = two_shoots
        (a / "DJI_0001_overlay.mp4").write_bytes(b"beside the source")

        response = await async_client.post(
            "/api/render/pre-check",
            json={"files": [{"video_path": str(a / "DJI_0001.mp4")}]},
        )
        assert len(response.json()["overwrite_conflicts"]) == 1


class TestDuplicateOutputDetection:
    async def test_two_inputs_claiming_one_output_are_reported(self, async_client, two_shoots):
        a, b, out = two_shoots
        response = await async_client.post(
            "/api/render/pre-check",
            json={
                "files": [
                    {"video_path": str(a / "DJI_0001.mp4")},
                    {"video_path": str(b / "DJI_0001.mp4")},
                ],
                "output_dir": str(out),
            },
        )
        dupes = response.json()["duplicate_outputs"]
        assert len(dupes) == 1
        assert Path(dupes[0]["output_path"]).name == "DJI_0001_overlay.mp4"
        assert len(dupes[0]["video_paths"]) == 2

    async def test_distinct_names_produce_no_duplicates(self, async_client, two_shoots):
        a, b, out = two_shoots
        (b / "DJI_0002.mp4").write_bytes(b"fake")
        response = await async_client.post(
            "/api/render/pre-check",
            json={
                "files": [
                    {"video_path": str(a / "DJI_0001.mp4")},
                    {"video_path": str(b / "DJI_0002.mp4")},
                ],
                "output_dir": str(out),
            },
        )
        assert response.json()["duplicate_outputs"] == []

    async def test_no_duplicates_without_an_output_dir(self, async_client, two_shoots):
        """Beside-the-source outputs cannot collide, which is why this is new."""
        a, b, _ = two_shoots
        response = await async_client.post(
            "/api/render/pre-check",
            json={
                "files": [
                    {"video_path": str(a / "DJI_0001.mp4")},
                    {"video_path": str(b / "DJI_0001.mp4")},
                ]
            },
        )
        assert response.json()["duplicate_outputs"] == []
