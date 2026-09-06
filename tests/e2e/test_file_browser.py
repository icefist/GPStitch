"""Browse button in the FILES panel.

The native dialog itself lives outside the browser and cannot be driven by
Playwright, so /api/browse is intercepted: these tests cover the wiring
between the button, the request, and the path input.
"""

import json

import pytest
from playwright.sync_api import Page, expect


def _stub_browse(page: Page, path: str | None):
    """Answer /api/browse as if the user picked `path` (or cancelled)."""
    page.route(
        "**/api/browse",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"path": path}),
        ),
    )


@pytest.mark.e2e
class TestBrowseButton:
    def test_browse_buttons_render_for_both_fields(self, app_page: Page):
        expect(app_page.locator("#video-browse-btn")).to_be_visible()
        expect(app_page.locator("#gps-browse-btn")).to_be_visible()

    def test_choosing_a_video_fills_the_video_path_input(self, app_page: Page):
        _stub_browse(app_page, "/Users/me/clip.mp4")
        app_page.route("**/api/local-file", lambda route: route.abort())

        app_page.locator("#video-browse-btn").click()
        expect(app_page.locator("#video-path-input")).to_have_value("/Users/me/clip.mp4")

    def test_choosing_a_gps_file_fills_the_gps_path_input(self, app_page: Page):
        _stub_browse(app_page, "/Users/me/track.gpx")
        app_page.route("**/api/local-file", lambda route: route.abort())

        app_page.locator("#gps-browse-btn").click()
        expect(app_page.locator("#gps-path-input")).to_have_value("/Users/me/track.gpx")

    def test_the_request_says_which_field_asked(self, app_page: Page):
        seen = {}

        def handler(route):
            seen["kind"] = route.request.post_data_json["kind"]
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"path": None}))

        app_page.route("**/api/browse", handler)
        app_page.locator("#gps-browse-btn").click()
        app_page.wait_for_timeout(300)
        assert seen["kind"] == "gps"

    def test_cancelling_leaves_the_input_untouched(self, app_page: Page):
        app_page.locator("#video-path-input").fill("/previous/value.mp4")
        _stub_browse(app_page, None)

        app_page.locator("#video-browse-btn").click()
        app_page.wait_for_timeout(300)
        expect(app_page.locator("#video-path-input")).to_have_value("/previous/value.mp4")

    def test_choosing_a_file_triggers_the_existing_load_flow(self, app_page: Page):
        """Browse should not require a second click on Load."""
        loaded = {}

        def on_load(route):
            loaded["path"] = route.request.post_data_json.get("file_path")
            route.abort()

        _stub_browse(app_page, "/Users/me/clip.mp4")
        app_page.route("**/api/local-file", on_load)

        app_page.locator("#video-browse-btn").click()
        app_page.wait_for_timeout(500)
        assert loaded.get("path") == "/Users/me/clip.mp4"
