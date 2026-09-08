"""A multi-GB upload must show how far it has got.

`fetch` reports nothing until the whole request body has been sent, so uploads
were silent from click to finish. They go through XMLHttpRequest now, whose
`upload.progress` events fill a bar in the drop zone.

Drop zones only render when the server is not in local mode. The frontend learns
that from GET /api/config, so the flag is flipped in the browser rather than by
starting a second server.
"""

import re

import pytest
from playwright.sync_api import Page, expect


def _as_upload_mode(page: Page):
    """Re-render the file panel with drop zones instead of path inputs."""
    page.route(
        "**/api/config",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"local_mode": false}',
        ),
    )
    page.reload()
    page.wait_for_load_state("networkidle")


@pytest.mark.e2e
class TestUploadProgress:
    def test_the_drop_zone_shows_a_progress_bar_while_uploading(self, app_page: Page, tmp_path):
        _as_upload_mode(app_page)
        # Left unanswered, so the in-flight state stays observable.
        app_page.route("**/api/upload", lambda route: None)

        video = tmp_path / "clip.mp4"
        video.write_bytes(b"\x00" * 2048)
        app_page.locator("#video-file-input").set_input_files(str(video))

        expect(app_page.locator("#video-drop-zone")).to_have_class(re.compile(r"\buploading\b"))
        expect(app_page.locator("#video-drop-zone .upload-progress-bar")).to_be_attached()

    def test_the_zone_names_the_file_being_uploaded(self, app_page: Page, tmp_path):
        _as_upload_mode(app_page)
        app_page.route("**/api/upload", lambda route: None)

        video = tmp_path / "clip.mp4"
        video.write_bytes(b"\x00" * 2048)
        app_page.locator("#video-file-input").set_input_files(str(video))

        expect(app_page.locator("#video-drop-zone .drop-zone-text")).to_contain_text("clip.mp4")

    def test_the_bar_is_taken_down_when_the_upload_fails(self, app_page: Page, tmp_path):
        """A bar left at 40% forever reads as a hung upload."""
        _as_upload_mode(app_page)
        app_page.route(
            "**/api/upload",
            lambda route: route.fulfill(status=500, content_type="application/json", body='{"detail": "boom"}'),
        )
        app_page.on("dialog", lambda dialog: dialog.dismiss())

        video = tmp_path / "clip.mp4"
        video.write_bytes(b"\x00" * 2048)
        app_page.locator("#video-file-input").set_input_files(str(video))

        expect(app_page.locator("#video-drop-zone")).not_to_have_class(re.compile(r"\buploading\b"))
        expect(app_page.locator("#video-drop-zone .drop-zone-text")).to_contain_text("Drop MP4/MOV")
