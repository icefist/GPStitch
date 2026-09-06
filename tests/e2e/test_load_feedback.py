"""Load must show it is working.

Loading a video can take a long time - a large clip on an external volume
means real I/O - and the button previously gave no sign it had been pressed.
The request is delayed here so the in-flight state is observable.
"""

import json

import pytest
from playwright.sync_api import Page, expect


def _hanging_local_file(page: Page):
    """Leave /api/local-file unanswered, so the in-flight state stays observable.

    Sleeping inside a sync route handler is no good: it blocks Playwright's own
    loop, so the assertion only runs once the request has already finished.
    """
    page.route("**/api/local-file", lambda route: None)


def _answered_local_file(page: Page):
    """Answer /api/local-file immediately, to assert the state afterwards."""
    page.route(
        "**/api/local-file",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"success": False, "message": "stubbed"}),
        ),
    )


@pytest.mark.e2e
class TestLoadShowsProgress:
    def test_button_is_disabled_while_loading(self, app_page: Page):
        """Otherwise a second click queues another multi-minute read."""
        _hanging_local_file(app_page)
        app_page.locator("#video-path-input").fill("/Users/me/clip.mp4")
        app_page.locator("#video-load-btn").click()

        expect(app_page.locator("#video-load-btn")).to_be_disabled()

    def test_button_says_it_is_working(self, app_page: Page):
        _hanging_local_file(app_page)
        app_page.locator("#video-path-input").fill("/Users/me/clip.mp4")
        app_page.locator("#video-load-btn").click()

        expect(app_page.locator("#video-load-btn")).not_to_have_text("Load")

    def test_button_is_restored_afterwards(self, app_page: Page):
        _answered_local_file(app_page)
        app_page.locator("#video-path-input").fill("/Users/me/clip.mp4")
        app_page.locator("#video-load-btn").click()

        expect(app_page.locator("#video-load-btn")).to_be_enabled(timeout=10_000)
        expect(app_page.locator("#video-load-btn")).to_have_text("Load")

    def test_button_is_restored_after_a_failure(self, app_page: Page):
        """A failed load must not leave the button stuck forever."""
        app_page.once("dialog", lambda d: d.dismiss())
        app_page.route(
            "**/api/local-file",
            lambda route: route.fulfill(
                status=404,
                content_type="application/json",
                body=json.dumps({"detail": "File not found"}),
            ),
        )
        app_page.locator("#video-path-input").fill("/Users/me/missing.mp4")
        app_page.locator("#video-load-btn").click()

        expect(app_page.locator("#video-load-btn")).to_be_enabled(timeout=10_000)
        expect(app_page.locator("#video-load-btn")).to_have_text("Load")

    def test_gps_load_button_behaves_the_same(self, app_page: Page):
        _hanging_local_file(app_page)
        app_page.locator("#gps-path-input").fill("/Users/me/track.gpx")
        app_page.locator("#gps-load-btn").click()

        expect(app_page.locator("#gps-load-btn")).to_be_disabled()
