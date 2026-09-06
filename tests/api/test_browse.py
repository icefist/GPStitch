"""Tests for the native file-browser endpoint.

The picker itself is patched out; no test opens a real dialog.
"""

from unittest.mock import AsyncMock, patch

import pytest

from gpstitch.services.file_dialog import FileDialogBusy, FileDialogUnavailable


class TestBrowseEndpoint:
    async def test_returns_the_chosen_path(self, async_client):
        with patch("gpstitch.api.browse.choose_file", AsyncMock(return_value="/Users/me/clip.mp4")):
            response = await async_client.post("/api/browse", json={"kind": "video"})
        assert response.status_code == 200
        assert response.json()["path"] == "/Users/me/clip.mp4"

    async def test_cancel_returns_null_path_not_an_error(self, async_client):
        """Dismissing the dialog is a normal outcome."""
        with patch("gpstitch.api.browse.choose_file", AsyncMock(return_value=None)):
            response = await async_client.post("/api/browse", json={"kind": "gps"})
        assert response.status_code == 200
        assert response.json()["path"] is None

    async def test_kind_is_passed_through(self, async_client):
        mock = AsyncMock(return_value=None)
        with patch("gpstitch.api.browse.choose_file", mock):
            await async_client.post("/api/browse", json={"kind": "gps"})
        assert mock.call_args[0][0] == "gps"

    async def test_unknown_kind_is_rejected(self, async_client):
        response = await async_client.post("/api/browse", json={"kind": "spreadsheet"})
        assert response.status_code == 422

    async def test_unsupported_platform_returns_501(self, async_client):
        with patch(
            "gpstitch.api.browse.choose_file",
            AsyncMock(side_effect=FileDialogUnavailable("no picker")),
        ):
            response = await async_client.post("/api/browse", json={"kind": "video"})
        assert response.status_code == 501

    async def test_second_concurrent_dialog_returns_409(self, async_client):
        with patch(
            "gpstitch.api.browse.choose_file",
            AsyncMock(side_effect=FileDialogBusy("already open")),
        ):
            response = await async_client.post("/api/browse", json={"kind": "video"})
        assert response.status_code == 409

    async def test_dialog_failure_returns_500(self, async_client):
        with patch("gpstitch.api.browse.choose_file", AsyncMock(side_effect=RuntimeError("boom"))):
            response = await async_client.post("/api/browse", json={"kind": "video"})
        assert response.status_code == 500

    async def test_failure_detail_is_not_doubled(self, async_client):
        """The detail reaches the user in an alert, so it must read cleanly."""
        with patch(
            "gpstitch.api.browse.choose_file",
            AsyncMock(side_effect=RuntimeError("File dialog failed")),
        ):
            response = await async_client.post("/api/browse", json={"kind": "video"})
        detail = response.json()["detail"]
        assert detail.lower().count("file dialog failed") == 1, detail

    async def test_failure_detail_keeps_the_underlying_reason(self, async_client):
        with patch("gpstitch.api.browse.choose_file", AsyncMock(side_effect=RuntimeError("zenity missing"))):
            response = await async_client.post("/api/browse", json={"kind": "video"})
        assert "zenity missing" in response.json()["detail"]

    async def test_availability_is_reported_for_this_platform(self, async_client):
        """The UI hides the Browse button where no picker exists."""
        response = await async_client.get("/api/browse/available")
        assert response.status_code == 200
        assert isinstance(response.json()["available"], bool)


@pytest.mark.parametrize("kind", ["video", "gps"])
async def test_both_field_kinds_are_accepted(async_client, kind):
    with patch("gpstitch.api.browse.choose_file", AsyncMock(return_value=None)):
        response = await async_client.post("/api/browse", json={"kind": kind})
    assert response.status_code == 200
